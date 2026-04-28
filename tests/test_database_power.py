"""Tests for the 6 new power features of the Database module.

Covers:
    1. EXPLAIN / query plan
    2. Cursor-based pagination (fetch_paginated)
    3. Schema diff
    4. Query history
    5. Connection health monitoring (ping + auto-reconnect)
    6. Read replica routing
"""

from __future__ import annotations

import pytest

from digitorn.modules.database.adapters.sql import SQLAdapter
from digitorn.modules.database.module import (
    DatabaseModule,
    _compute_schema_diff,
    _schema_to_snapshot,
)
from digitorn.modules.database.params import (
    ConnectParams,
    ExecuteQueryParams,
    ExplainQueryParams,
    FetchPaginatedParams,
    FetchResultsParams,
    PingParams,
    QueryHistoryParams,
    SchemaDiffParams,
)


# ═══════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════


@pytest.fixture
async def module():
    m = DatabaseModule()
    await m.on_start()
    yield m
    await m.on_stop()


@pytest.fixture
async def module_with_db(module: DatabaseModule):
    """Module with a connected SQLite database and test data."""
    await module.connect(ConnectParams(
        connection_id="db", driver="sqlite", database=":memory:",
    ))
    await module.execute_query(ExecuteQueryParams(
        connection_id="db",
        query="CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, age INTEGER)",
    ))
    for i in range(20):
        await module.execute_query(ExecuteQueryParams(
            connection_id="db",
            query="INSERT INTO users (name, age) VALUES (:p0, :p1)",
            params=[f"user_{i}", 20 + i],
        ))
    return module


# ═══════════════════════════════════════════════════════════════════════
# 1. EXPLAIN / Query Plan
# ═══════════════════════════════════════════════════════════════════════


class TestExplainQuery:

    @pytest.mark.asyncio
    async def test_explain_select(self, module_with_db: DatabaseModule):
        result = await module_with_db.explain_query(ExplainQueryParams(
            connection_id="db",
            query="SELECT * FROM users WHERE age > 25",
        ))
        assert result.success
        assert result.data["query"] == "SELECT * FROM users WHERE age > 25"
        assert result.data["analyze"] is False
        assert isinstance(result.data["plan"], list)
        assert len(result.data["plan"]) > 0

    @pytest.mark.asyncio
    async def test_explain_missing_connection(self, module: DatabaseModule):
        result = await module.explain_query(ExplainQueryParams(
            connection_id="nonexistent",
            query="SELECT 1",
        ))
        assert not result.success

    @pytest.mark.asyncio
    async def test_explain_adapter_directly(self):
        adapter = SQLAdapter()
        await adapter.connect("sqlite+aiosqlite:///:memory:")
        try:
            await adapter.execute(
                "CREATE TABLE t (id INTEGER PRIMARY KEY, val TEXT)"
            )
            plan = await adapter.explain("SELECT * FROM t")
            assert isinstance(plan, list)
            assert len(plan) > 0
        finally:
            await adapter.disconnect()

    @pytest.mark.asyncio
    async def test_explain_records_in_history(self, module_with_db: DatabaseModule):
        await module_with_db.explain_query(ExplainQueryParams(
            connection_id="db",
            query="SELECT * FROM users",
        ))
        result = await module_with_db.query_history(QueryHistoryParams(limit=5))
        queries = [e["query"] for e in result.data["entries"]]
        assert any("EXPLAIN" in q for q in queries)


# ═══════════════════════════════════════════════════════════════════════
# 2. Cursor-based Pagination
# ═══════════════════════════════════════════════════════════════════════


class TestFetchPaginated:

    @pytest.mark.asyncio
    async def test_first_page(self, module_with_db: DatabaseModule):
        result = await module_with_db.fetch_paginated(FetchPaginatedParams(
            connection_id="db",
            query="SELECT * FROM users ORDER BY id",
            page_size=5,
            cursor=0,
        ))
        assert result.success
        assert result.data["count"] == 5
        assert result.data["has_more"] is True
        assert result.data["next_cursor"] == 5
        assert result.data["cursor"] == 0

    @pytest.mark.asyncio
    async def test_second_page(self, module_with_db: DatabaseModule):
        result = await module_with_db.fetch_paginated(FetchPaginatedParams(
            connection_id="db",
            query="SELECT * FROM users ORDER BY id",
            page_size=5,
            cursor=5,
        ))
        assert result.success
        assert result.data["count"] == 5
        assert result.data["has_more"] is True
        assert result.data["next_cursor"] == 10

    @pytest.mark.asyncio
    async def test_last_page(self, module_with_db: DatabaseModule):
        result = await module_with_db.fetch_paginated(FetchPaginatedParams(
            connection_id="db",
            query="SELECT * FROM users ORDER BY id",
            page_size=5,
            cursor=15,
        ))
        assert result.success
        assert result.data["count"] == 5
        assert result.data["has_more"] is False
        assert result.data["next_cursor"] is None

    @pytest.mark.asyncio
    async def test_beyond_last_page(self, module_with_db: DatabaseModule):
        result = await module_with_db.fetch_paginated(FetchPaginatedParams(
            connection_id="db",
            query="SELECT * FROM users ORDER BY id",
            page_size=5,
            cursor=20,
        ))
        assert result.success
        assert result.data["count"] == 0
        assert result.data["has_more"] is False

    @pytest.mark.asyncio
    async def test_page_size_larger_than_data(self, module_with_db: DatabaseModule):
        result = await module_with_db.fetch_paginated(FetchPaginatedParams(
            connection_id="db",
            query="SELECT * FROM users ORDER BY id",
            page_size=100,
            cursor=0,
        ))
        assert result.success
        assert result.data["count"] == 20
        assert result.data["has_more"] is False

    @pytest.mark.asyncio
    async def test_missing_connection(self, module: DatabaseModule):
        result = await module.fetch_paginated(FetchPaginatedParams(
            connection_id="nope",
            query="SELECT 1",
            page_size=10,
        ))
        assert not result.success


# ═══════════════════════════════════════════════════════════════════════
# 3. Schema Diff
# ═══════════════════════════════════════════════════════════════════════


class TestSchemaDiff:

    @pytest.mark.asyncio
    async def test_save_snapshot(self, module_with_db: DatabaseModule):
        result = await module_with_db.schema_diff(SchemaDiffParams(
            connection_id="db", save_snapshot=True,
        ))
        assert result.success
        assert result.data["snapshot_saved"] is True
        assert result.data["table_count"] >= 1

    @pytest.mark.asyncio
    async def test_no_baseline_error(self, module_with_db: DatabaseModule):
        result = await module_with_db.schema_diff(SchemaDiffParams(
            connection_id="db", save_snapshot=False,
        ))
        assert not result.success
        assert "baseline" in result.error.lower()

    @pytest.mark.asyncio
    async def test_no_changes(self, module_with_db: DatabaseModule):
        await module_with_db.schema_diff(SchemaDiffParams(
            connection_id="db", save_snapshot=True,
        ))
        result = await module_with_db.schema_diff(SchemaDiffParams(
            connection_id="db", save_snapshot=False,
        ))
        assert result.success
        assert result.data["has_changes"] is False

    @pytest.mark.asyncio
    async def test_detect_added_table(self, module_with_db: DatabaseModule):
        await module_with_db.schema_diff(SchemaDiffParams(
            connection_id="db", save_snapshot=True,
        ))

        # Add a new table
        await module_with_db.execute_query(ExecuteQueryParams(
            connection_id="db",
            query="CREATE TABLE orders (id INTEGER PRIMARY KEY, total REAL)",
        ))

        result = await module_with_db.schema_diff(SchemaDiffParams(
            connection_id="db", save_snapshot=False,
        ))
        assert result.success
        assert result.data["has_changes"] is True
        assert "orders" in result.data["diff"]["tables_added"]

    @pytest.mark.asyncio
    async def test_detect_removed_table(self, module_with_db: DatabaseModule):
        # Create extra table first
        await module_with_db.execute_query(ExecuteQueryParams(
            connection_id="db",
            query="CREATE TABLE temp_table (id INTEGER)",
        ))
        await module_with_db.schema_diff(SchemaDiffParams(
            connection_id="db", save_snapshot=True,
        ))

        # Drop the table
        await module_with_db.execute_query(ExecuteQueryParams(
            connection_id="db",
            query="DROP TABLE temp_table",
        ))

        result = await module_with_db.schema_diff(SchemaDiffParams(
            connection_id="db", save_snapshot=False,
        ))
        assert result.success
        assert result.data["has_changes"] is True
        assert "temp_table" in result.data["diff"]["tables_removed"]

    @pytest.mark.asyncio
    async def test_detect_added_column(self, module_with_db: DatabaseModule):
        await module_with_db.schema_diff(SchemaDiffParams(
            connection_id="db", save_snapshot=True,
        ))

        # Add a column
        await module_with_db.execute_query(ExecuteQueryParams(
            connection_id="db",
            query="ALTER TABLE users ADD COLUMN email TEXT",
        ))

        result = await module_with_db.schema_diff(SchemaDiffParams(
            connection_id="db", save_snapshot=False,
        ))
        assert result.success
        assert result.data["has_changes"] is True
        modified = result.data["diff"]["tables_modified"]
        assert len(modified) >= 1
        users_mod = [m for m in modified if m["table"] == "users"][0]
        assert "email" in users_mod["columns_added"]


class TestSchemaDiffHelpers:

    def test_compute_diff_no_changes(self):
        snap = {"t1": {"columns": {"id": {"type": "int", "nullable": False, "primary_key": True}}, "indexes": [], "foreign_keys": []}}
        diff = _compute_schema_diff(snap, snap)
        assert diff["tables_added"] == []
        assert diff["tables_removed"] == []
        assert diff["tables_modified"] == []

    def test_compute_diff_added_table(self):
        old = {"t1": {"columns": {}, "indexes": [], "foreign_keys": []}}
        new = {**old, "t2": {"columns": {}, "indexes": [], "foreign_keys": []}}
        diff = _compute_schema_diff(old, new)
        assert diff["tables_added"] == ["t2"]

    def test_compute_diff_removed_table(self):
        old = {"t1": {"columns": {}, "indexes": [], "foreign_keys": []}, "t2": {"columns": {}, "indexes": [], "foreign_keys": []}}
        new = {"t1": {"columns": {}, "indexes": [], "foreign_keys": []}}
        diff = _compute_schema_diff(old, new)
        assert diff["tables_removed"] == ["t2"]

    def test_compute_diff_modified_column(self):
        old = {"t1": {"columns": {"id": {"type": "int", "nullable": False, "primary_key": True}}, "indexes": [], "foreign_keys": []}}
        new = {"t1": {"columns": {"id": {"type": "bigint", "nullable": False, "primary_key": True}}, "indexes": [], "foreign_keys": []}}
        diff = _compute_schema_diff(old, new)
        assert len(diff["tables_modified"]) == 1
        assert diff["tables_modified"][0]["columns_modified"][0]["column"] == "id"


# ═══════════════════════════════════════════════════════════════════════
# 4. Query History
# ═══════════════════════════════════════════════════════════════════════


class TestQueryHistory:

    @pytest.mark.asyncio
    async def test_history_records_queries(self, module_with_db: DatabaseModule):
        # module_with_db already ran CREATE + 20 INSERTs
        result = await module_with_db.query_history(QueryHistoryParams(limit=50))
        assert result.success
        assert result.data["count"] > 0
        assert result.data["total_in_buffer"] > 0

    @pytest.mark.asyncio
    async def test_history_filter_by_connection(self, module_with_db: DatabaseModule):
        result = await module_with_db.query_history(QueryHistoryParams(
            connection_id="db", limit=10,
        ))
        assert result.success
        for entry in result.data["entries"]:
            assert entry["connection_id"] == "db"

    @pytest.mark.asyncio
    async def test_history_filter_by_connection_missing(self, module_with_db: DatabaseModule):
        result = await module_with_db.query_history(QueryHistoryParams(
            connection_id="nonexistent", limit=10,
        ))
        assert result.success
        assert result.data["count"] == 0

    @pytest.mark.asyncio
    async def test_history_errors_only(self, module_with_db: DatabaseModule):
        # Execute a bad query to create an error
        await module_with_db.execute_query(ExecuteQueryParams(
            connection_id="db",
            query="SELECT * FROM nonexistent_table",
        ))

        result = await module_with_db.query_history(QueryHistoryParams(
            errors_only=True, limit=10,
        ))
        assert result.success
        for entry in result.data["entries"]:
            assert entry["success"] is False

    @pytest.mark.asyncio
    async def test_history_includes_timing(self, module_with_db: DatabaseModule):
        await module_with_db.fetch_results(FetchResultsParams(
            connection_id="db",
            query="SELECT * FROM users LIMIT 5",
        ))
        result = await module_with_db.query_history(QueryHistoryParams(limit=1))
        entry = result.data["entries"][0]
        assert "duration_ms" in entry
        assert "timestamp" in entry

    @pytest.mark.asyncio
    async def test_history_circular_buffer(self, module: DatabaseModule):
        """History should not exceed _QUERY_HISTORY_MAX."""
        await module.connect(ConnectParams(
            connection_id="db", driver="sqlite", database=":memory:",
        ))
        await module.execute_query(ExecuteQueryParams(
            connection_id="db",
            query="CREATE TABLE t (id INTEGER)",
        ))
        # Fill beyond max
        for i in range(600):
            module._record_query("db", f"SELECT {i}")

        assert len(module._query_history) <= module._QUERY_HISTORY_MAX

    @pytest.mark.asyncio
    async def test_history_most_recent_first(self, module_with_db: DatabaseModule):
        result = await module_with_db.query_history(QueryHistoryParams(limit=5))
        entries = result.data["entries"]
        if len(entries) >= 2:
            # Most recent should be first
            assert entries[0]["timestamp"] >= entries[-1]["timestamp"]


# ═══════════════════════════════════════════════════════════════════════
# 5. Connection Health Monitoring (Ping)
# ═══════════════════════════════════════════════════════════════════════


class TestPing:

    @pytest.mark.asyncio
    async def test_ping_alive(self, module_with_db: DatabaseModule):
        result = await module_with_db.ping(PingParams(connection_id="db"))
        assert result.success
        assert result.data["alive"] is True

    @pytest.mark.asyncio
    async def test_ping_missing_connection(self, module: DatabaseModule):
        result = await module.ping(PingParams(connection_id="nope"))
        assert result.success  # ping itself succeeds, but reports not alive
        assert result.data["alive"] is False

    @pytest.mark.asyncio
    async def test_ping_all(self, module: DatabaseModule):
        await module.connect(ConnectParams(
            connection_id="a", driver="sqlite", database=":memory:",
        ))
        await module.connect(ConnectParams(
            connection_id="b", driver="sqlite", database=":memory:",
        ))
        result = await module.ping(PingParams(connection_id="*"))
        assert result.success
        assert result.data["all_alive"] is True
        assert "a" in result.data["connections"]
        assert "b" in result.data["connections"]

    @pytest.mark.asyncio
    async def test_ping_adapter_directly(self):
        adapter = SQLAdapter()
        # Not connected yet
        assert await adapter.ping() is False

        await adapter.connect("sqlite+aiosqlite:///:memory:")
        assert await adapter.ping() is True
        await adapter.disconnect()

    @pytest.mark.asyncio
    async def test_ping_reconnect_with_saved_config(self, module: DatabaseModule):
        """Ping with reconnect should work for persistent connections."""
        await module.connect(ConnectParams(
            connection_id="persist_db",
            driver="sqlite",
            database=":memory:",
            persist=True,
        ))
        result = await module.ping(PingParams(
            connection_id="persist_db", reconnect=True,
        ))
        assert result.success
        assert result.data["alive"] is True


# ═══════════════════════════════════════════════════════════════════════
# 6. Read Replica Routing
# ═══════════════════════════════════════════════════════════════════════


class TestReplicaRouting:

    @pytest.mark.asyncio
    async def test_connect_with_group_and_role(self, module: DatabaseModule):
        result = await module.connect(ConnectParams(
            connection_id="primary",
            driver="sqlite",
            database=":memory:",
            group="prod",
            role="primary",
        ))
        assert result.success
        assert result.data["role"] == "primary"
        assert result.data["group"] == "prod"
        assert "prod" in module._connection_groups
        assert module._connection_groups["prod"]["primary"] == "primary"

    @pytest.mark.asyncio
    async def test_connect_replica(self, module: DatabaseModule):
        await module.connect(ConnectParams(
            connection_id="primary",
            driver="sqlite",
            database=":memory:",
            group="prod",
            role="primary",
        ))
        await module.connect(ConnectParams(
            connection_id="replica1",
            driver="sqlite",
            database=":memory:",
            group="prod",
            role="replica",
        ))
        assert "replica1" in module._connection_groups["prod"]["replicas"]

    @pytest.mark.asyncio
    async def test_resolve_connection_for_read_routes_to_replica(self, module: DatabaseModule):
        await module.connect(ConnectParams(
            connection_id="primary",
            driver="sqlite",
            database=":memory:",
            group="prod",
            role="primary",
        ))
        await module.connect(ConnectParams(
            connection_id="replica1",
            driver="sqlite",
            database=":memory:",
            group="prod",
            role="replica",
        ))

        resolved = module._resolve_connection_for_read("primary")
        assert resolved == "replica1"

    @pytest.mark.asyncio
    async def test_resolve_round_robin(self, module: DatabaseModule):
        await module.connect(ConnectParams(
            connection_id="primary",
            driver="sqlite",
            database=":memory:",
            group="prod",
            role="primary",
        ))
        await module.connect(ConnectParams(
            connection_id="r1",
            driver="sqlite",
            database=":memory:",
            group="prod",
            role="replica",
        ))
        await module.connect(ConnectParams(
            connection_id="r2",
            driver="sqlite",
            database=":memory:",
            group="prod",
            role="replica",
        ))

        resolved = []
        for _ in range(4):
            resolved.append(module._resolve_connection_for_read("primary"))

        # Should alternate between r1 and r2
        assert "r1" in resolved
        assert "r2" in resolved

    @pytest.mark.asyncio
    async def test_no_group_returns_same_connection(self, module: DatabaseModule):
        await module.connect(ConnectParams(
            connection_id="standalone",
            driver="sqlite",
            database=":memory:",
        ))
        resolved = module._resolve_connection_for_read("standalone")
        assert resolved == "standalone"

    @pytest.mark.asyncio
    async def test_fetch_uses_replica(self, module: DatabaseModule):
        """fetch_results on a primary should route to replica."""
        # Set up primary with data
        await module.connect(ConnectParams(
            connection_id="primary",
            driver="sqlite",
            database=":memory:",
            group="prod",
            role="primary",
        ))
        await module.execute_query(ExecuteQueryParams(
            connection_id="primary",
            query="CREATE TABLE t (id INTEGER PRIMARY KEY, val TEXT)",
        ))
        await module.execute_query(ExecuteQueryParams(
            connection_id="primary",
            query="INSERT INTO t (val) VALUES (:p0)",
            params=["hello"],
        ))

        # Replica is a separate in-memory DB, so query will work but return 0 rows
        # This tests the routing mechanism, not data replication
        await module.connect(ConnectParams(
            connection_id="replica1",
            driver="sqlite",
            database=":memory:",
            group="prod",
            role="replica",
        ))
        # Create same table on replica for the query to succeed
        await module.execute_query(ExecuteQueryParams(
            connection_id="replica1",
            query="CREATE TABLE t (id INTEGER PRIMARY KEY, val TEXT)",
        ))
        await module.execute_query(ExecuteQueryParams(
            connection_id="replica1",
            query="INSERT INTO t (val) VALUES (:p0)",
            params=["from_replica"],
        ))

        result = await module.fetch_results(FetchResultsParams(
            connection_id="primary",
            query="SELECT * FROM t",
        ))
        assert result.success
        # Should have routed to replica1 - verify by checking data
        assert result.data["rows"][0]["val"] == "from_replica"


# ═══════════════════════════════════════════════════════════════════════
# State persistence
# ═══════════════════════════════════════════════════════════════════════


class TestStatePersistence:

    @pytest.mark.asyncio
    async def test_snapshot_includes_new_fields(self, module_with_db: DatabaseModule):
        # Save a schema snapshot
        await module_with_db.schema_diff(SchemaDiffParams(
            connection_id="db", save_snapshot=True,
        ))

        snapshot = module_with_db.state_snapshot()
        assert "query_history" in snapshot
        assert "schema_snapshots" in snapshot
        assert "connection_groups" in snapshot
        assert len(snapshot["query_history"]) > 0
        assert "db" in snapshot["schema_snapshots"]

    @pytest.mark.asyncio
    async def test_restore_preserves_history_and_snapshots(self):
        m1 = DatabaseModule()
        await m1.on_start()
        await m1.connect(ConnectParams(
            connection_id="db", driver="sqlite", database=":memory:",
        ))
        await m1.execute_query(ExecuteQueryParams(
            connection_id="db",
            query="CREATE TABLE t (id INTEGER)",
        ))
        await m1.schema_diff(SchemaDiffParams(
            connection_id="db", save_snapshot=True,
        ))
        snapshot = m1.state_snapshot()
        await m1.on_stop()

        m2 = DatabaseModule()
        await m2.on_start()
        await m2.restore_state(snapshot)

        assert len(m2._query_history) > 0
        assert "db" in m2._schema_snapshots
        await m2.on_stop()
