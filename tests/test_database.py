"""Tests for the Database module - adapters, connection pool, and module actions.

Covers:
    - SQLAdapter: connect, execute, fetch, introspect, transactions, watcher contract
    - ConnectionPool: named connections, URL building, disconnect_all
    - DatabaseModule: all actions via direct instantiation (SQLite in-memory)
"""

from __future__ import annotations

import pytest

from digitorn.modules.database.adapters.base import (
    ColumnInfo,
    DatabaseAdapter,
    ExecuteResult,
    FetchResult,
    ItemChecksum,
    SchemaInfo,
    TableInfo,
    TableStats,
    WatchItem,
)
from digitorn.modules.database.adapters.sql import SQLAdapter, _quote_ident, _sanitize_url
from digitorn.modules.database.connections import ConnectionPool
from digitorn.modules.database.module import DatabaseModule
from digitorn.modules.database.params import (
    AnnotateParams,
    BeginTransactionParams,
    CommitTransactionParams,
    ConnectParams,
    DescribeParams,
    DisconnectParams,
    ExecuteQueryParams,
    ExtractForIndexParams,
    FetchResultsParams,
    GetAnnotationsParams,
    GetTableSchemaParams,
    IntrospectParams,
    ListConnectionsParams,
    ListTablesParams,
    RollbackTransactionParams,
    SampleParams,
    TableStatsParams,
)


# ═══════════════════════════════════════════════════════════════════════
# SQLAdapter - Unit Tests
# ═══════════════════════════════════════════════════════════════════════


class TestSQLAdapter:
    """Test the SQLAlchemy async adapter with SQLite in-memory."""

    @pytest.fixture
    async def adapter(self):
        a = SQLAdapter()
        await a.connect("sqlite+aiosqlite:///:memory:")
        yield a
        await a.disconnect()

    @pytest.mark.asyncio
    async def test_connect_and_properties(self, adapter: SQLAdapter):
        assert adapter.connected is True
        assert adapter.driver_name == "sqlite"

    @pytest.mark.asyncio
    async def test_disconnect(self):
        a = SQLAdapter()
        await a.connect("sqlite+aiosqlite:///:memory:")
        assert a.connected
        await a.disconnect()
        assert not a.connected

    @pytest.mark.asyncio
    async def test_execute_create_and_insert(self, adapter: SQLAdapter):
        result = await adapter.execute(
            "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT NOT NULL)"
        )
        assert isinstance(result, ExecuteResult)

        result = await adapter.execute(
            "INSERT INTO users (name) VALUES (:p0)", ["Alice"]
        )
        assert result.rows_affected == 1

    @pytest.mark.asyncio
    async def test_fetch_rows(self, adapter: SQLAdapter):
        await adapter.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, label TEXT)")
        await adapter.execute("INSERT INTO items (label) VALUES (:p0)", ["alpha"])
        await adapter.execute("INSERT INTO items (label) VALUES (:p0)", ["beta"])

        result = await adapter.fetch("SELECT * FROM items")
        assert isinstance(result, FetchResult)
        assert result.columns == ["id", "label"]
        assert len(result.rows) == 2
        assert result.rows[0]["label"] == "alpha"

    @pytest.mark.asyncio
    async def test_fetch_with_limit(self, adapter: SQLAdapter):
        await adapter.execute("CREATE TABLE nums (n INTEGER)")
        for i in range(10):
            await adapter.execute("INSERT INTO nums (n) VALUES (:p0)", [i])

        result = await adapter.fetch("SELECT * FROM nums", limit=3)
        assert len(result.rows) == 3

    @pytest.mark.asyncio
    async def test_introspect(self, adapter: SQLAdapter):
        await adapter.execute(
            "CREATE TABLE products (id INTEGER PRIMARY KEY, name TEXT, price REAL)"
        )
        schema = await adapter.introspect()
        assert isinstance(schema, SchemaInfo)
        assert schema.driver == "sqlite"
        assert len(schema.tables) == 1
        assert schema.tables[0].name == "products"
        assert len(schema.tables[0].columns) == 3

    @pytest.mark.asyncio
    async def test_list_tables(self, adapter: SQLAdapter):
        await adapter.execute("CREATE TABLE t1 (id INTEGER PRIMARY KEY)")
        await adapter.execute("CREATE TABLE t2 (id INTEGER PRIMARY KEY)")
        tables = await adapter.list_tables()
        assert len(tables) == 2
        names = {t.name for t in tables}
        assert names == {"t1", "t2"}

    @pytest.mark.asyncio
    async def test_get_columns(self, adapter: SQLAdapter):
        await adapter.execute(
            "CREATE TABLE orders (id INTEGER PRIMARY KEY, total REAL, status TEXT)"
        )
        columns = await adapter.get_columns("orders")
        assert len(columns) == 3
        assert all(isinstance(c, ColumnInfo) for c in columns)
        names = [c.name for c in columns]
        assert "id" in names
        assert "total" in names

    @pytest.mark.asyncio
    async def test_table_stats(self, adapter: SQLAdapter):
        await adapter.execute("CREATE TABLE logs (msg TEXT)")
        for i in range(5):
            await adapter.execute("INSERT INTO logs (msg) VALUES (:p0)", [f"log {i}"])
        stats = await adapter.table_stats("logs")
        assert isinstance(stats, TableStats)
        assert stats.name == "logs"
        assert stats.row_count == 5

    @pytest.mark.asyncio
    async def test_sample(self, adapter: SQLAdapter):
        await adapter.execute("CREATE TABLE data (val INTEGER)")
        for i in range(20):
            await adapter.execute("INSERT INTO data (val) VALUES (:p0)", [i])
        result = await adapter.sample("data", limit=3)
        assert len(result.rows) == 3

    @pytest.mark.asyncio
    async def test_transaction_commit(self, adapter: SQLAdapter):
        await adapter.execute("CREATE TABLE tx (val TEXT)")
        await adapter.begin()
        await adapter.execute("INSERT INTO tx (val) VALUES (:p0)", ["committed"])
        await adapter.commit()

        result = await adapter.fetch("SELECT * FROM tx")
        assert len(result.rows) == 1
        assert result.rows[0]["val"] == "committed"

    @pytest.mark.asyncio
    async def test_transaction_rollback(self, adapter: SQLAdapter):
        await adapter.execute("CREATE TABLE tx2 (val TEXT)")
        await adapter.execute("INSERT INTO tx2 (val) VALUES (:p0)", ["keep"])

        await adapter.begin()
        await adapter.execute("INSERT INTO tx2 (val) VALUES (:p0)", ["discard"])
        await adapter.rollback()

        result = await adapter.fetch("SELECT * FROM tx2")
        assert len(result.rows) == 1
        assert result.rows[0]["val"] == "keep"

    @pytest.mark.asyncio
    async def test_watcher_list_items(self, adapter: SQLAdapter):
        await adapter.execute("CREATE TABLE watch1 (id INTEGER)")
        await adapter.execute("CREATE TABLE watch2 (id INTEGER)")
        items = await adapter.list_items()
        assert len(items) == 2
        assert all(isinstance(i, WatchItem) for i in items)

    @pytest.mark.asyncio
    async def test_watcher_list_items_with_pattern(self, adapter: SQLAdapter):
        await adapter.execute("CREATE TABLE users (id INTEGER)")
        await adapter.execute("CREATE TABLE logs (id INTEGER)")
        items = await adapter.list_items(patterns=["*users*"])
        assert len(items) == 1
        assert items[0].id == "users"

    @pytest.mark.asyncio
    async def test_watcher_checksum(self, adapter: SQLAdapter):
        await adapter.execute("CREATE TABLE cs (id INTEGER, name TEXT)")
        await adapter.execute("INSERT INTO cs (id, name) VALUES (:p0, :p1)", [1, "a"])

        checksums = await adapter.checksum(["cs"])
        assert len(checksums) == 1
        assert isinstance(checksums[0], ItemChecksum)
        assert checksums[0].id == "cs"
        assert len(checksums[0].hash) == 16

        # Insert another row - checksum should change
        await adapter.execute("INSERT INTO cs (id, name) VALUES (:p0, :p1)", [2, "b"])
        checksums2 = await adapter.checksum(["cs"])
        assert checksums2[0].hash != checksums[0].hash

    @pytest.mark.asyncio
    async def test_checksum_missing_table(self, adapter: SQLAdapter):
        checksums = await adapter.checksum(["nonexistent"])
        assert len(checksums) == 1
        assert checksums[0].hash == ""

    @pytest.mark.asyncio
    async def test_not_connected_raises(self):
        a = SQLAdapter()
        with pytest.raises(RuntimeError, match="Not connected"):
            await a.execute("SELECT 1")

    @pytest.mark.asyncio
    async def test_protocol_compliance(self):
        assert isinstance(SQLAdapter(), DatabaseAdapter)


# ═══════════════════════════════════════════════════════════════════════
# Helpers - Unit Tests
# ═══════════════════════════════════════════════════════════════════════


class TestHelpers:
    def test_sanitize_url_with_password(self):
        url = "postgresql+asyncpg://user:secret@localhost/db"
        assert _sanitize_url(url) == "postgresql+asyncpg://user:****@localhost/db"

    def test_sanitize_url_without_password(self):
        url = "sqlite+aiosqlite:///test.db"
        assert _sanitize_url(url) == url

    def test_quote_ident_valid(self):
        assert _quote_ident("users") == '"users"'
        assert _quote_ident("public.users") == '"public.users"'

    def test_quote_ident_invalid(self):
        with pytest.raises(ValueError, match="Invalid identifier"):
            _quote_ident("users; DROP TABLE--")


# ═══════════════════════════════════════════════════════════════════════
# ConnectionPool - Unit Tests
# ═══════════════════════════════════════════════════════════════════════


class TestConnectionPool:

    @pytest.mark.asyncio
    async def test_connect_and_get(self):
        pool = ConnectionPool()
        entry = await pool.connect("test", "sqlite", database=":memory:")
        assert entry.connection_id == "test"
        assert entry.driver == "sqlite"
        assert "test" in pool
        assert len(pool) == 1
        await pool.disconnect_all()

    @pytest.mark.asyncio
    async def test_get_adapter(self):
        pool = ConnectionPool()
        await pool.connect("db", "sqlite", database=":memory:")
        adapter = pool.get_adapter("db")
        assert adapter.connected
        await pool.disconnect_all()

    @pytest.mark.asyncio
    async def test_get_missing_raises(self):
        pool = ConnectionPool()
        with pytest.raises(KeyError, match="not found"):
            pool.get("missing")

    @pytest.mark.asyncio
    async def test_disconnect_single(self):
        pool = ConnectionPool()
        await pool.connect("a", "sqlite", database=":memory:")
        await pool.connect("b", "sqlite", database=":memory:")
        assert len(pool) == 2
        await pool.disconnect("a")
        assert len(pool) == 1
        assert "a" not in pool
        assert "b" in pool
        await pool.disconnect_all()

    @pytest.mark.asyncio
    async def test_disconnect_all(self):
        pool = ConnectionPool()
        await pool.connect("x", "sqlite", database=":memory:")
        await pool.connect("y", "sqlite", database=":memory:")
        closed = await pool.disconnect_all()
        assert closed == 2
        assert len(pool) == 0

    @pytest.mark.asyncio
    async def test_reconnect_same_id(self):
        pool = ConnectionPool()
        await pool.connect("db", "sqlite", database=":memory:")
        # Connecting again with same ID should close old and open new
        await pool.connect("db", "sqlite", database=":memory:")
        assert len(pool) == 1
        await pool.disconnect_all()

    @pytest.mark.asyncio
    async def test_list_connections(self):
        pool = ConnectionPool()
        await pool.connect("main", "sqlite", database=":memory:")
        conns = pool.list_connections()
        assert len(conns) == 1
        assert conns[0]["connection_id"] == "main"
        assert conns[0]["driver"] == "sqlite"
        assert conns[0]["connected"] is True
        assert "age_seconds" in conns[0]
        await pool.disconnect_all()

    @pytest.mark.asyncio
    async def test_connect_with_url(self):
        pool = ConnectionPool()
        entry = await pool.connect(
            "direct", "sqlite", url="sqlite+aiosqlite:///:memory:"
        )
        assert entry.adapter.connected
        await pool.disconnect_all()

    def test_build_url_sqlite(self):
        import os
        url = ConnectionPool._build_url("sqlite", "test.db", None, None, None, None)
        expected = f"sqlite+aiosqlite:///{os.path.realpath('test.db')}"
        assert url == expected

    def test_build_url_sqlite_memory(self):
        url = ConnectionPool._build_url("sqlite", None, None, None, None, None)
        assert url == "sqlite+aiosqlite:///:memory:"

    def test_build_url_postgresql(self):
        url = ConnectionPool._build_url(
            "postgresql", "mydb", "dbhost", 5432, "admin", "pass",
            allowed_hosts=["dbhost"],
        )
        assert url == "postgresql+asyncpg://admin:pass@dbhost:5432/mydb"

    def test_build_url_no_database_raises(self):
        with pytest.raises(ValueError, match="database is required"):
            ConnectionPool._build_url(
                "postgresql", None, "host", None, None, None,
                allowed_hosts=["host"],
            )

    def test_unsupported_driver_raises(self):
        with pytest.raises(ValueError, match="No URL scheme"):
            ConnectionPool._build_url("cassandra", None, None, None, None, None)


# ═══════════════════════════════════════════════════════════════════════
# DatabaseModule - Action Tests (SQLite in-memory)
# ═══════════════════════════════════════════════════════════════════════


class TestDatabaseModule:
    """Test all module actions using SQLite in-memory database."""

    @pytest.fixture
    async def module(self):
        m = DatabaseModule()
        await m.on_start()
        yield m
        await m.on_stop()

    @pytest.mark.asyncio
    async def test_connect_action(self, module: DatabaseModule):
        result = await module.connect(ConnectParams(
            connection_id="test", driver="sqlite", database=":memory:",
        ))
        assert result.success
        assert result.data["connection_id"] == "test"
        assert result.data["driver"] == "sqlite"

    @pytest.mark.asyncio
    async def test_connect_invalid_driver(self, module: DatabaseModule):
        result = await module.connect(ConnectParams(
            connection_id="bad", driver="sqlite", url="invalid://url",
        ))
        assert not result.success

    @pytest.mark.asyncio
    async def test_disconnect_action(self, module: DatabaseModule):
        await module.connect(ConnectParams(
            connection_id="temp", driver="sqlite", database=":memory:",
        ))
        result = await module.disconnect(DisconnectParams(connection_id="temp"))
        assert result.success
        assert result.data["connection_id"] == "temp"

    @pytest.mark.asyncio
    async def test_disconnect_missing(self, module: DatabaseModule):
        result = await module.disconnect(DisconnectParams(connection_id="nope"))
        assert not result.success

    @pytest.mark.asyncio
    async def test_disconnect_all(self, module: DatabaseModule):
        await module.connect(ConnectParams(
            connection_id="a", driver="sqlite", database=":memory:",
        ))
        await module.connect(ConnectParams(
            connection_id="b", driver="sqlite", database=":memory:",
        ))
        result = await module.disconnect(DisconnectParams(connection_id="*"))
        assert result.success
        assert result.data["closed"] == 2

    @pytest.mark.asyncio
    async def test_list_connections_action(self, module: DatabaseModule):
        await module.connect(ConnectParams(
            connection_id="db1", driver="sqlite", database=":memory:",
        ))
        result = await module.list_connections(ListConnectionsParams())
        assert result.success
        assert result.data["count"] == 1
        assert result.data["connections"][0]["connection_id"] == "db1"

    @pytest.mark.asyncio
    async def test_execute_and_fetch(self, module: DatabaseModule):
        await module.connect(ConnectParams(
            connection_id="db", driver="sqlite", database=":memory:",
        ))

        # Create table
        result = await module.execute_query(ExecuteQueryParams(
            connection_id="db",
            query="CREATE TABLE test (id INTEGER PRIMARY KEY, val TEXT)",
        ))
        assert result.success

        # Insert
        result = await module.execute_query(ExecuteQueryParams(
            connection_id="db",
            query="INSERT INTO test (val) VALUES (:p0)",
            params=["hello"],
        ))
        assert result.success
        assert result.data["rows_affected"] == 1

        # Fetch
        result = await module.fetch_results(FetchResultsParams(
            connection_id="db", query="SELECT * FROM test",
        ))
        assert result.success
        assert result.data["count"] == 1
        assert result.data["rows"][0]["val"] == "hello"

    @pytest.mark.asyncio
    async def test_execute_query_missing_connection(self, module: DatabaseModule):
        result = await module.execute_query(ExecuteQueryParams(
            connection_id="missing", query="SELECT 1",
        ))
        assert not result.success

    @pytest.mark.asyncio
    async def test_list_tables_action(self, module: DatabaseModule):
        await module.connect(ConnectParams(
            connection_id="db", driver="sqlite", database=":memory:",
        ))
        await module.execute_query(ExecuteQueryParams(
            connection_id="db",
            query="CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)",
        ))
        result = await module.list_tables(ListTablesParams(connection_id="db"))
        assert result.success
        assert result.data["count"] == 1
        assert result.data["tables"][0]["name"] == "users"

    @pytest.mark.asyncio
    async def test_get_table_schema_action(self, module: DatabaseModule):
        await module.connect(ConnectParams(
            connection_id="db", driver="sqlite", database=":memory:",
        ))
        await module.execute_query(ExecuteQueryParams(
            connection_id="db",
            query="CREATE TABLE orders (id INTEGER PRIMARY KEY, total REAL, status TEXT)",
        ))
        result = await module.get_table_schema(GetTableSchemaParams(
            connection_id="db", table="orders",
        ))
        assert result.success
        assert result.data["column_count"] == 3
        names = [c["name"] for c in result.data["columns"]]
        assert "total" in names

    @pytest.mark.asyncio
    async def test_table_stats_action(self, module: DatabaseModule):
        await module.connect(ConnectParams(
            connection_id="db", driver="sqlite", database=":memory:",
        ))
        await module.execute_query(ExecuteQueryParams(
            connection_id="db", query="CREATE TABLE rows (n INTEGER)",
        ))
        for i in range(7):
            await module.execute_query(ExecuteQueryParams(
                connection_id="db",
                query="INSERT INTO rows (n) VALUES (:p0)",
                params=[i],
            ))
        result = await module.table_stats(TableStatsParams(
            connection_id="db", table="rows",
        ))
        assert result.success
        assert result.data["row_count"] == 7

    @pytest.mark.asyncio
    async def test_sample_action(self, module: DatabaseModule):
        await module.connect(ConnectParams(
            connection_id="db", driver="sqlite", database=":memory:",
        ))
        await module.execute_query(ExecuteQueryParams(
            connection_id="db", query="CREATE TABLE s (v INTEGER)",
        ))
        for i in range(20):
            await module.execute_query(ExecuteQueryParams(
                connection_id="db",
                query="INSERT INTO s (v) VALUES (:p0)",
                params=[i],
            ))
        result = await module.sample(SampleParams(
            connection_id="db", table="s", limit=3,
        ))
        assert result.success
        assert result.data["count"] == 3

    @pytest.mark.asyncio
    async def test_introspect_action(self, module: DatabaseModule):
        await module.connect(ConnectParams(
            connection_id="db", driver="sqlite", database=":memory:",
        ))
        await module.execute_query(ExecuteQueryParams(
            connection_id="db",
            query="CREATE TABLE a (id INTEGER PRIMARY KEY)",
        ))
        await module.execute_query(ExecuteQueryParams(
            connection_id="db",
            query="CREATE TABLE b (id INTEGER PRIMARY KEY, a_id INTEGER REFERENCES a(id))",
        ))
        result = await module.introspect(IntrospectParams(connection_id="db"))
        assert result.success
        assert result.data["table_count"] == 2
        assert result.data["driver"] == "sqlite"

    @pytest.mark.asyncio
    async def test_transaction_actions(self, module: DatabaseModule):
        await module.connect(ConnectParams(
            connection_id="db", driver="sqlite", database=":memory:",
        ))
        await module.execute_query(ExecuteQueryParams(
            connection_id="db", query="CREATE TABLE tx (val TEXT)",
        ))

        # Begin + insert + rollback → no data
        result = await module.begin_transaction(BeginTransactionParams(connection_id="db"))
        assert result.success

        await module.execute_query(ExecuteQueryParams(
            connection_id="db",
            query="INSERT INTO tx (val) VALUES (:p0)",
            params=["rolled_back"],
        ))
        result = await module.rollback_transaction(RollbackTransactionParams(connection_id="db"))
        assert result.success

        fetch = await module.fetch_results(FetchResultsParams(
            connection_id="db", query="SELECT * FROM tx",
        ))
        assert fetch.data["count"] == 0

        # Begin + insert + commit → data persisted
        await module.begin_transaction(BeginTransactionParams(connection_id="db"))
        await module.execute_query(ExecuteQueryParams(
            connection_id="db",
            query="INSERT INTO tx (val) VALUES (:p0)",
            params=["committed"],
        ))
        await module.commit_transaction(CommitTransactionParams(connection_id="db"))

        fetch = await module.fetch_results(FetchResultsParams(
            connection_id="db", query="SELECT * FROM tx",
        ))
        assert fetch.data["count"] == 1
        assert fetch.data["rows"][0]["val"] == "committed"

    @pytest.mark.asyncio
    async def test_health_check(self, module: DatabaseModule):
        health = await module.health_check()
        assert health["status"] == "healthy"
        assert health["connection_count"] == 0

        await module.connect(ConnectParams(
            connection_id="hc", driver="sqlite", database=":memory:",
        ))
        health = await module.health_check()
        assert health["connection_count"] == 1

    @pytest.mark.asyncio
    async def test_get_manifest(self, module: DatabaseModule):
        manifest = module.get_manifest()
        assert manifest.module_id == "database"
        assert "database:read" in manifest.declared_permissions
        assert "database:write" in manifest.declared_permissions
        assert "database:admin" in manifest.declared_permissions

    @pytest.mark.asyncio
    async def test_state_snapshot(self, module: DatabaseModule):
        await module.connect(ConnectParams(
            connection_id="snap", driver="sqlite", database=":memory:",
        ))
        snapshot = module.state_snapshot()
        assert "connections" in snapshot
        assert len(snapshot["connections"]) == 1

    # ── describe action ──────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_describe_action(self, module: DatabaseModule):
        await module.connect(ConnectParams(
            connection_id="db", driver="sqlite", database=":memory:",
        ))
        await module.execute_query(ExecuteQueryParams(
            connection_id="db",
            query=(
                "CREATE TABLE products ("
                "  id INTEGER PRIMARY KEY,"
                "  name TEXT NOT NULL,"
                "  price REAL DEFAULT 0.0,"
                "  category_id INTEGER"
                ")"
            ),
        ))
        await module.execute_query(ExecuteQueryParams(
            connection_id="db",
            query="INSERT INTO products (name, price) VALUES (:p0, :p1)",
            params=["Widget", 9.99],
        ))
        await module.execute_query(ExecuteQueryParams(
            connection_id="db",
            query="INSERT INTO products (name, price) VALUES (:p0, :p1)",
            params=["Gadget", 24.50],
        ))

        result = await module.describe(DescribeParams(
            connection_id="db", table="products", sample_limit=5,
        ))
        assert result.success
        data = result.data

        # Schema
        assert data["table"] == "products"
        assert data["column_count"] == 4
        col_names = [c["name"] for c in data["columns"]]
        assert "id" in col_names
        assert "name" in col_names
        assert "price" in col_names

        # Stats
        assert data["row_count"] == 2

        # Sample
        assert data["sample"]["count"] == 2
        assert data["sample"]["rows"][0]["name"] == "Widget"

    @pytest.mark.asyncio
    async def test_describe_no_sample(self, module: DatabaseModule):
        await module.connect(ConnectParams(
            connection_id="db", driver="sqlite", database=":memory:",
        ))
        await module.execute_query(ExecuteQueryParams(
            connection_id="db",
            query="CREATE TABLE empty (id INTEGER PRIMARY KEY)",
        ))
        result = await module.describe(DescribeParams(
            connection_id="db", table="empty", sample_limit=0,
        ))
        assert result.success
        assert result.data["sample"]["count"] == 0
        assert result.data["sample"]["rows"] == []

    @pytest.mark.asyncio
    async def test_describe_missing_connection(self, module: DatabaseModule):
        result = await module.describe(DescribeParams(
            connection_id="nope", table="t",
        ))
        assert not result.success

    # ── extract_for_index action ─────────────────────────────────────

    @pytest.mark.asyncio
    async def test_extract_for_index(self, module: DatabaseModule):
        await module.connect(ConnectParams(
            connection_id="idx", driver="sqlite", database=":memory:",
        ))
        await module.execute_query(ExecuteQueryParams(
            connection_id="idx",
            query=(
                "CREATE TABLE users ("
                "  id INTEGER PRIMARY KEY,"
                "  email TEXT NOT NULL,"
                "  name TEXT"
                ")"
            ),
        ))
        await module.execute_query(ExecuteQueryParams(
            connection_id="idx",
            query=(
                "CREATE TABLE orders ("
                "  id INTEGER PRIMARY KEY,"
                "  user_id INTEGER REFERENCES users(id),"
                "  total REAL"
                ")"
            ),
        ))
        # Insert some data for row count
        await module.execute_query(ExecuteQueryParams(
            connection_id="idx",
            query="INSERT INTO users (email, name) VALUES (:p0, :p1)",
            params=["alice@test.com", "Alice"],
        ))

        result = await module.extract_for_index(ExtractForIndexParams(
            source_id="test-db", root="idx",
        ))
        assert result.success
        data = result.data

        entries = data["entries"]
        relations = data["relations"]

        # Should have 2 table entries + 3 columns (users) + 3 columns (orders) = 8
        table_entries = [e for e in entries if e["kind"] == "table"]
        column_entries = [e for e in entries if e["kind"] == "column"]
        assert len(table_entries) == 2
        assert len(column_entries) == 6

        # Table entries have correct names
        table_names = {e["name"] for e in table_entries}
        assert table_names == {"users", "orders"}

        # Users table has row_count in metadata
        users_entry = next(e for e in table_entries if e["name"] == "users")
        assert users_entry["metadata"]["row_count"] == 1

        # Contains relations (table → column)
        contains_rels = [r for r in relations if r["kind"] == "contains"]
        assert len(contains_rels) == 6

        # FK relation (orders → users)
        fk_rels = [r for r in relations if r["kind"] == "references"]
        assert len(fk_rels) == 1
        assert fk_rels[0]["metadata"]["referred_table"] == "users"

    @pytest.mark.asyncio
    async def test_extract_for_index_missing_connection(self, module: DatabaseModule):
        result = await module.extract_for_index(ExtractForIndexParams(
            source_id="test", root="missing",
        ))
        assert not result.success


# ═══════════════════════════════════════════════════════════════════════
# Annotations - Tests
# ═══════════════════════════════════════════════════════════════════════


class TestAnnotations:
    """Test business annotations on tables and columns."""

    @pytest.fixture
    async def module(self):
        m = DatabaseModule()
        await m.on_start()
        await m.connect(ConnectParams(
            connection_id="db", driver="sqlite", database=":memory:",
        ))
        await m.execute_query(ExecuteQueryParams(
            connection_id="db",
            query=(
                "CREATE TABLE orders ("
                "  id INTEGER PRIMARY KEY,"
                "  customer_id INTEGER,"
                "  status TEXT DEFAULT 'draft',"
                "  total REAL,"
                "  usr_crt_dt TEXT"
                ")"
            ),
        ))
        yield m
        await m.on_stop()

    @pytest.mark.asyncio
    async def test_annotate_table(self, module: DatabaseModule):
        result = await module.annotate(AnnotateParams(
            connection_id="db",
            table="orders",
            description="Customer orders. Status transitions: draft → pending → shipped → delivered.",
            tags=["financial", "audit-log"],
            rules=["Cannot delete if status != draft", "total must be >= 0"],
        ))
        assert result.success
        assert result.data["target"] == "orders"
        assert "description" in result.data["annotation"]

    @pytest.mark.asyncio
    async def test_annotate_column(self, module: DatabaseModule):
        result = await module.annotate(AnnotateParams(
            connection_id="db",
            table="orders",
            column="usr_crt_dt",
            description="User creation date in DD/MM/YYYY format.",
            tags=["pii"],
        ))
        assert result.success
        assert result.data["target"] == "orders.usr_crt_dt"

    @pytest.mark.asyncio
    async def test_annotate_glossary(self, module: DatabaseModule):
        result = await module.annotate(AnnotateParams(
            connection_id="db",
            table="orders",
            glossary={"SKU": "Stock Keeping Unit", "MRR": "Monthly Recurring Revenue"},
        ))
        assert result.success
        assert result.data["annotation"]["glossary"]["SKU"] == "Stock Keeping Unit"

    @pytest.mark.asyncio
    async def test_annotate_merge(self, module: DatabaseModule):
        # First annotation
        await module.annotate(AnnotateParams(
            connection_id="db", table="orders",
            description="Customer orders.",
            tags=["financial"],
        ))
        # Second annotation - adds rules, keeps description
        await module.annotate(AnnotateParams(
            connection_id="db", table="orders",
            rules=["Cannot delete if shipped"],
        ))
        result = await module.get_annotations(GetAnnotationsParams(
            connection_id="db", table="orders",
        ))
        table_ann = result.data["annotations"]["__table__"]
        assert table_ann["description"] == "Customer orders."
        assert table_ann["rules"] == ["Cannot delete if shipped"]
        assert table_ann["tags"] == ["financial"]

    @pytest.mark.asyncio
    async def test_get_annotations_all(self, module: DatabaseModule):
        await module.annotate(AnnotateParams(
            connection_id="db", table="orders",
            description="Orders table.",
        ))
        result = await module.get_annotations(GetAnnotationsParams(
            connection_id="db",
        ))
        assert result.success
        assert "orders" in result.data["annotations"]

    @pytest.mark.asyncio
    async def test_get_annotations_table(self, module: DatabaseModule):
        await module.annotate(AnnotateParams(
            connection_id="db", table="orders",
            description="Orders table.",
        ))
        await module.annotate(AnnotateParams(
            connection_id="db", table="orders", column="status",
            description="Order lifecycle status.",
        ))
        result = await module.get_annotations(GetAnnotationsParams(
            connection_id="db", table="orders",
        ))
        assert "__table__" in result.data["annotations"]
        assert "status" in result.data["annotations"]

    @pytest.mark.asyncio
    async def test_get_annotations_column(self, module: DatabaseModule):
        await module.annotate(AnnotateParams(
            connection_id="db", table="orders", column="total",
            description="Order total in USD.",
        ))
        result = await module.get_annotations(GetAnnotationsParams(
            connection_id="db", table="orders", column="total",
        ))
        assert result.data["annotation"]["description"] == "Order total in USD."

    @pytest.mark.asyncio
    async def test_describe_includes_annotations(self, module: DatabaseModule):
        # Annotate table and column
        await module.annotate(AnnotateParams(
            connection_id="db", table="orders",
            description="Customer orders.",
            tags=["financial"],
        ))
        await module.annotate(AnnotateParams(
            connection_id="db", table="orders", column="status",
            description="Order lifecycle status: draft → pending → shipped → delivered.",
        ))

        # Describe should include annotations
        result = await module.describe(DescribeParams(
            connection_id="db", table="orders", sample_limit=0,
        ))
        assert result.success

        # Table-level annotation
        assert result.data["business_context"]["description"] == "Customer orders."
        assert result.data["business_context"]["tags"] == ["financial"]

        # Column-level annotation
        status_col = next(
            c for c in result.data["columns"] if c["name"] == "status"
        )
        assert "business_context" in status_col
        assert "lifecycle" in status_col["business_context"]["description"]

    @pytest.mark.asyncio
    async def test_annotations_in_extract_for_index(self, module: DatabaseModule):
        await module.annotate(AnnotateParams(
            connection_id="db", table="orders",
            description="Customer orders table.",
            tags=["financial"],
            rules=["total >= 0"],
        ))
        await module.annotate(AnnotateParams(
            connection_id="db", table="orders", column="status",
            description="Order lifecycle status.",
        ))

        result = await module.extract_for_index(ExtractForIndexParams(
            source_id="ann-test", root="db",
        ))
        assert result.success

        entries = result.data["entries"]
        table_entry = next(e for e in entries if e["kind"] == "table")
        assert table_entry["summary"] == "Customer orders table."
        assert table_entry["metadata"]["tags"] == ["financial"]
        assert table_entry["metadata"]["rules"] == ["total >= 0"]

        status_entry = next(
            e for e in entries
            if e["kind"] == "column" and e["name"] == "orders.status"
        )
        assert status_entry["summary"] == "Order lifecycle status."

    @pytest.mark.asyncio
    async def test_annotations_persist_in_snapshot(self, module: DatabaseModule):
        await module.annotate(AnnotateParams(
            connection_id="db", table="orders",
            description="Persisted annotation.",
        ))
        snapshot = module.state_snapshot()
        assert "annotations" in snapshot
        assert "db" in snapshot["annotations"]
        assert "orders" in snapshot["annotations"]["db"]


# ═══════════════════════════════════════════════════════════════════════
# Gateway - Auto-reconnect Tests
# ═══════════════════════════════════════════════════════════════════════


class TestGateway:
    """Test persistent connections and auto-reconnect."""

    @pytest.mark.asyncio
    async def test_persist_connection_config(self):
        m = DatabaseModule()
        await m.on_start()

        result = await m.connect(ConnectParams(
            connection_id="main",
            driver="sqlite",
            database=":memory:",
            persist=True,
        ))
        assert result.success
        assert result.data["persistent"] is True

        # Config is saved
        snapshot = m.state_snapshot()
        assert len(snapshot["saved_configs"]) == 1
        cfg = snapshot["saved_configs"][0]
        assert cfg["connection_id"] == "main"
        assert cfg["driver"] == "sqlite"
        # No plaintext password
        assert "password" not in cfg

        await m.on_stop()

    @pytest.mark.asyncio
    async def test_non_persist_connection(self):
        m = DatabaseModule()
        await m.on_start()

        result = await m.connect(ConnectParams(
            connection_id="temp",
            driver="sqlite",
            database=":memory:",
        ))
        assert result.success
        assert result.data["persistent"] is False

        snapshot = m.state_snapshot()
        assert len(snapshot["saved_configs"]) == 0

        await m.on_stop()

    @pytest.mark.asyncio
    async def test_auto_reconnect_on_restore(self):
        # Phase 1: connect with persist
        m1 = DatabaseModule()
        await m1.on_start()
        await m1.connect(ConnectParams(
            connection_id="db",
            driver="sqlite",
            database=":memory:",
            persist=True,
        ))
        snapshot = m1.state_snapshot()
        await m1.on_stop()

        # Phase 2: new module, restore state
        m2 = DatabaseModule()
        await m2.on_start()
        await m2.restore_state(snapshot)

        # Connection should be restored
        assert "db" in m2.pool
        adapter = m2.pool.get_adapter("db")
        assert adapter.connected

        await m2.on_stop()

    @pytest.mark.asyncio
    async def test_auto_reconnect_with_env_password(self, monkeypatch):
        monkeypatch.setenv("TEST_DB_PASS", "secret123")

        m = DatabaseModule()
        await m.on_start()

        # Save a config with password_env
        m._saved_configs = [{
            "connection_id": "secured",
            "driver": "sqlite",
            "database": ":memory:",
            "password_env": "TEST_DB_PASS",
        }]
        state = m.state_snapshot()
        await m.on_stop()

        # Restore - should resolve env var
        m2 = DatabaseModule()
        await m2.on_start()
        await m2.restore_state(state)
        assert "secured" in m2.pool

        await m2.on_stop()

    @pytest.mark.asyncio
    async def test_disconnect_removes_saved_config(self):
        m = DatabaseModule()
        await m.on_start()

        await m.connect(ConnectParams(
            connection_id="db", driver="sqlite",
            database=":memory:", persist=True,
        ))
        assert len(m._saved_configs) == 1

        await m.disconnect(DisconnectParams(connection_id="db"))
        assert len(m._saved_configs) == 0

        await m.on_stop()

    @pytest.mark.asyncio
    async def test_disconnect_all_clears_saved_configs(self):
        m = DatabaseModule()
        await m.on_start()

        await m.connect(ConnectParams(
            connection_id="a", driver="sqlite",
            database=":memory:", persist=True,
        ))
        await m.connect(ConnectParams(
            connection_id="b", driver="sqlite",
            database=":memory:", persist=True,
        ))
        assert len(m._saved_configs) == 2

        await m.disconnect(DisconnectParams(connection_id="*"))
        assert len(m._saved_configs) == 0

        await m.on_stop()

    @pytest.mark.asyncio
    async def test_annotations_survive_restart(self):
        # Phase 1: annotate
        m1 = DatabaseModule()
        await m1.on_start()
        await m1.connect(ConnectParams(
            connection_id="db", driver="sqlite",
            database=":memory:", persist=True,
        ))
        await m1.execute_query(ExecuteQueryParams(
            connection_id="db",
            query="CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT)",
        ))
        await m1.annotate(AnnotateParams(
            connection_id="db", table="users",
            description="User accounts.",
        ))
        snapshot = m1.state_snapshot()
        await m1.on_stop()

        # Phase 2: restore
        m2 = DatabaseModule()
        await m2.on_start()
        await m2.restore_state(snapshot)

        # Annotations should be restored
        result = await m2.get_annotations(GetAnnotationsParams(
            connection_id="db", table="users",
        ))
        assert result.data["annotations"]["__table__"]["description"] == "User accounts."

        await m2.on_stop()
