"""Tests for database security - DatabasePolicy, QueryGuard, AuditLogger, and module integration."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from digitorn.modules.database.security import (
    AuditEntry,
    AuditLogger,
    DatabasePolicy,
    QueryBlockedError,
    QueryGuard,
    _extract_statement_type,
    _extract_table_names,
    _sanitize_query,
    filter_table_list,
    redact_columns,
)


# ═══════════════════════════════════════════════════════════════════════
# DatabasePolicy tests
# ═══════════════════════════════════════════════════════════════════════


class TestDatabasePolicy:
    def test_default_policy(self):
        p = DatabasePolicy()
        assert p.read_only is False
        assert p.allowed_statements == []
        assert p.blocked_statements == []
        assert p.table_whitelist == []
        assert p.table_blacklist == []
        assert p.column_blacklist == {}
        assert p.max_rows_returned == 10_000
        assert p.max_query_time_seconds == 30.0
        assert p.allow_transactions is True
        assert p.blocked_operations == []
        assert p.blocked_commands == []

    def test_readonly_preset(self):
        p = DatabasePolicy.readonly()
        assert p.read_only is True
        assert p.allow_transactions is True

    def test_safe_write_preset(self):
        p = DatabasePolicy.safe_write()
        assert p.read_only is False
        assert "DROP" in p.blocked_statements
        assert "TRUNCATE" in p.blocked_statements
        assert "drop_collection" in p.blocked_operations
        assert "FLUSHDB" in p.blocked_commands

    def test_unrestricted_preset(self):
        p = DatabasePolicy.unrestricted()
        assert p.read_only is False
        assert p.blocked_statements == []

    def test_serialization_roundtrip(self):
        p = DatabasePolicy(
            read_only=True,
            table_blacklist=["secrets", "audit_*"],
            column_blacklist={"users": ["password_hash", "ssn"]},
        )
        data = p.model_dump()
        restored = DatabasePolicy(**data)
        assert restored.read_only is True
        assert restored.table_blacklist == ["secrets", "audit_*"]
        assert restored.column_blacklist == {"users": ["password_hash", "ssn"]}

    def test_json_roundtrip(self):
        p = DatabasePolicy.safe_write()
        json_str = p.model_dump_json()
        restored = DatabasePolicy.model_validate_json(json_str)
        assert restored.blocked_statements == p.blocked_statements


# ═══════════════════════════════════════════════════════════════════════
# QueryGuard tests
# ═══════════════════════════════════════════════════════════════════════


class TestQueryGuardSQL:
    def test_readonly_blocks_write(self):
        guard = QueryGuard(DatabasePolicy(read_only=True))
        with pytest.raises(QueryBlockedError, match="read-only") as exc_info:
            guard.check_sql_query("INSERT INTO users VALUES (1, 'x')")
        assert exc_info.value.policy_rule == "read_only"

    def test_readonly_allows_select(self):
        guard = QueryGuard(DatabasePolicy(read_only=True))
        guard.check_sql_query("SELECT * FROM users")  # Should not raise

    def test_readonly_blocks_drop(self):
        guard = QueryGuard(DatabasePolicy(read_only=True))
        with pytest.raises(QueryBlockedError):
            guard.check_sql_query("DROP TABLE users")

    def test_readonly_blocks_update(self):
        guard = QueryGuard(DatabasePolicy(read_only=True))
        with pytest.raises(QueryBlockedError):
            guard.check_sql_query("UPDATE users SET name='x' WHERE id=1")

    def test_readonly_blocks_delete(self):
        guard = QueryGuard(DatabasePolicy(read_only=True))
        with pytest.raises(QueryBlockedError):
            guard.check_sql_query("DELETE FROM users WHERE id=1")

    def test_allowed_statements_whitelist(self):
        guard = QueryGuard(DatabasePolicy(allowed_statements=["SELECT", "INSERT"]))
        guard.check_sql_query("SELECT * FROM users")
        guard.check_sql_query("INSERT INTO users VALUES (1)")
        with pytest.raises(QueryBlockedError, match="not in allowed list"):
            guard.check_sql_query("DELETE FROM users WHERE id=1")

    def test_blocked_statements_blacklist(self):
        guard = QueryGuard(DatabasePolicy(blocked_statements=["DROP", "TRUNCATE"]))
        guard.check_sql_query("SELECT * FROM users")
        guard.check_sql_query("INSERT INTO users VALUES (1)")
        with pytest.raises(QueryBlockedError, match="blocked by policy"):
            guard.check_sql_query("DROP TABLE users")
        with pytest.raises(QueryBlockedError):
            guard.check_sql_query("TRUNCATE TABLE users")

    def test_allowed_takes_precedence_over_blocked(self):
        guard = QueryGuard(DatabasePolicy(
            allowed_statements=["SELECT"],
            blocked_statements=["DROP"],
        ))
        # DROP is in blocked_statements, but allowed_statements takes precedence
        with pytest.raises(QueryBlockedError, match="not in allowed list"):
            guard.check_sql_query("DROP TABLE users")
        # INSERT is not in blocked_statements, but it's not in allowed either
        with pytest.raises(QueryBlockedError, match="not in allowed list"):
            guard.check_sql_query("INSERT INTO users VALUES (1)")

    def test_case_insensitive_statements(self):
        guard = QueryGuard(DatabasePolicy(blocked_statements=["drop"]))
        with pytest.raises(QueryBlockedError):
            guard.check_sql_query("DROP TABLE users")

    def test_query_with_comments(self):
        guard = QueryGuard(DatabasePolicy(read_only=True))
        # Comment before statement
        with pytest.raises(QueryBlockedError):
            guard.check_sql_query("-- this is a comment\nINSERT INTO users VALUES (1)")


class TestQueryGuardTableAccess:
    def test_table_whitelist(self):
        guard = QueryGuard(DatabasePolicy(table_whitelist=["users", "orders"]))
        guard.check_table_access("users")
        guard.check_table_access("orders")
        with pytest.raises(QueryBlockedError, match="not in whitelist"):
            guard.check_table_access("secrets")

    def test_table_whitelist_glob(self):
        guard = QueryGuard(DatabasePolicy(table_whitelist=["app_*"]))
        guard.check_table_access("app_users")
        guard.check_table_access("app_orders")
        with pytest.raises(QueryBlockedError):
            guard.check_table_access("internal_secrets")

    def test_table_blacklist(self):
        guard = QueryGuard(DatabasePolicy(table_blacklist=["secrets", "audit_log"]))
        guard.check_table_access("users")
        with pytest.raises(QueryBlockedError, match="blacklisted"):
            guard.check_table_access("secrets")
        with pytest.raises(QueryBlockedError):
            guard.check_table_access("audit_log")

    def test_table_blacklist_glob(self):
        guard = QueryGuard(DatabasePolicy(table_blacklist=["internal_*"]))
        guard.check_table_access("users")
        with pytest.raises(QueryBlockedError):
            guard.check_table_access("internal_secrets")
        with pytest.raises(QueryBlockedError):
            guard.check_table_access("internal_config")

    def test_sql_query_checks_tables(self):
        guard = QueryGuard(DatabasePolicy(table_blacklist=["secrets"]))
        with pytest.raises(QueryBlockedError, match="blacklisted"):
            guard.check_sql_query("SELECT * FROM secrets")

    def test_join_query_checks_all_tables(self):
        guard = QueryGuard(DatabasePolicy(table_blacklist=["secrets"]))
        with pytest.raises(QueryBlockedError):
            guard.check_sql_query("SELECT * FROM users JOIN secrets ON users.id = secrets.user_id")


class TestQueryGuardColumnAccess:
    def test_no_blocked_columns(self):
        guard = QueryGuard(DatabasePolicy())
        blocked = guard.check_column_access("users", ["id", "name", "email"])
        assert blocked == []

    def test_blocked_columns_detected(self):
        guard = QueryGuard(DatabasePolicy(
            column_blacklist={"users": ["password_hash", "ssn"]},
        ))
        blocked = guard.check_column_access("users", ["id", "name", "password_hash", "ssn"])
        assert set(blocked) == {"password_hash", "ssn"}

    def test_blocked_columns_other_table_unaffected(self):
        guard = QueryGuard(DatabasePolicy(
            column_blacklist={"users": ["password_hash"]},
        ))
        blocked = guard.check_column_access("orders", ["id", "password_hash"])
        assert blocked == []

    def test_effective_row_limit(self):
        guard = QueryGuard(DatabasePolicy(max_rows_returned=100))
        assert guard.get_effective_row_limit(1000) == 100
        assert guard.get_effective_row_limit(50) == 50
        assert guard.get_effective_row_limit(100) == 100


class TestQueryGuardMongo:
    def test_readonly_blocks_write_ops(self):
        guard = QueryGuard(DatabasePolicy(read_only=True))
        with pytest.raises(QueryBlockedError, match="read-only"):
            guard.check_mongo_operation("users", "insert_one")
        with pytest.raises(QueryBlockedError):
            guard.check_mongo_operation("users", "delete_many")

    def test_readonly_allows_read_ops(self):
        guard = QueryGuard(DatabasePolicy(read_only=True))
        # Non-write operations should pass
        guard.check_mongo_operation("users", "find")

    def test_blocked_operations(self):
        guard = QueryGuard(DatabasePolicy(blocked_operations=["drop_collection"]))
        guard.check_mongo_operation("users", "insert_one")
        with pytest.raises(QueryBlockedError, match="blocked by policy"):
            guard.check_mongo_operation("users", "drop_collection")

    def test_collection_access_check(self):
        guard = QueryGuard(DatabasePolicy(table_blacklist=["secrets"]))
        with pytest.raises(QueryBlockedError, match="blacklisted"):
            guard.check_mongo_operation("secrets", "find")


class TestQueryGuardRedis:
    def test_readonly_blocks_write_commands(self):
        guard = QueryGuard(DatabasePolicy(read_only=True))
        with pytest.raises(QueryBlockedError, match="read-only"):
            guard.check_redis_command("SET")
        with pytest.raises(QueryBlockedError):
            guard.check_redis_command("DEL")

    def test_readonly_allows_read_commands(self):
        guard = QueryGuard(DatabasePolicy(read_only=True))
        guard.check_redis_command("GET")
        guard.check_redis_command("KEYS")

    def test_blocked_commands(self):
        guard = QueryGuard(DatabasePolicy(blocked_commands=["FLUSHDB", "CONFIG"]))
        guard.check_redis_command("SET")
        with pytest.raises(QueryBlockedError, match="blocked by policy"):
            guard.check_redis_command("FLUSHDB")
        with pytest.raises(QueryBlockedError):
            guard.check_redis_command("CONFIG")

    def test_case_insensitive(self):
        guard = QueryGuard(DatabasePolicy(blocked_commands=["flushdb"]))
        with pytest.raises(QueryBlockedError):
            guard.check_redis_command("FLUSHDB")


class TestQueryGuardTransaction:
    def test_transactions_allowed_by_default(self):
        guard = QueryGuard(DatabasePolicy())
        guard.check_transaction_allowed()  # Should not raise

    def test_transactions_blocked(self):
        guard = QueryGuard(DatabasePolicy(allow_transactions=False))
        with pytest.raises(QueryBlockedError, match="disabled"):
            guard.check_transaction_allowed()


# ═══════════════════════════════════════════════════════════════════════
# AuditLogger tests
# ═══════════════════════════════════════════════════════════════════════


class TestAuditLogger:
    def test_log_creates_entry(self):
        audit = AuditLogger()
        entry = audit.log(
            connection_id="main",
            action="fetch_results",
            driver="sqlite",
            query="SELECT * FROM users",
            rows_affected=10,
            duration=0.05,
        )
        assert isinstance(entry, AuditEntry)
        assert entry.connection_id == "main"
        assert entry.action == "fetch_results"
        assert entry.rows_affected == 10
        assert entry.blocked is False

    def test_log_blocked_entry(self):
        audit = AuditLogger()
        entry = audit.log(
            connection_id="main",
            action="execute_query",
            query="DROP TABLE users",
            blocked=True,
            block_reason="read_only",
        )
        assert entry.blocked is True
        assert entry.block_reason == "read_only"

    def test_get_recent(self):
        audit = AuditLogger()
        for i in range(10):
            audit.log(connection_id="main", action=f"action_{i}", driver="sqlite")
        entries = audit.get_recent(5)
        assert len(entries) == 5
        # Most recent first
        assert entries[0]["action"] == "action_9"
        assert entries[4]["action"] == "action_5"

    def test_get_stats(self):
        audit = AuditLogger()
        audit.log(connection_id="main", action="fetch_results", driver="sqlite")
        audit.log(connection_id="main", action="execute_query", driver="sqlite")
        audit.log(connection_id="analytics", action="fetch_results", driver="postgresql")
        audit.log(connection_id="main", action="fetch_results", blocked=True, block_reason="x")

        stats = audit.get_stats()
        assert stats["total_operations"] == 4
        assert stats["blocked_operations"] == 1
        assert stats["by_action"]["fetch_results"] == 3
        assert stats["by_action"]["execute_query"] == 1
        assert stats["by_connection"]["main"] == 3
        assert stats["by_connection"]["analytics"] == 1

    def test_buffer_limit(self):
        audit = AuditLogger(max_buffer_size=5)
        for i in range(10):
            audit.log(connection_id="main", action=f"a_{i}", driver="sqlite")
        entries = audit.get_recent(10)
        assert len(entries) == 5
        assert entries[0]["action"] == "a_9"

    def test_clear(self):
        audit = AuditLogger()
        audit.log(connection_id="main", action="test", driver="sqlite")
        assert len(audit.get_recent(10)) == 1
        audit.clear()
        assert len(audit.get_recent(10)) == 0

    def test_query_sanitization(self):
        audit = AuditLogger()
        long_query = "SELECT " + "x, " * 200 + "y FROM users"
        entry = audit.log(connection_id="main", action="fetch", driver="sqlite", query=long_query)
        assert len(entry.query_preview) <= 203  # 200 + "..."


# ═══════════════════════════════════════════════════════════════════════
# Column redaction tests
# ═══════════════════════════════════════════════════════════════════════


class TestRedactColumns:
    def test_no_blocked_columns(self):
        rows = [{"id": 1, "name": "Alice"}]
        result = redact_columns(rows, [])
        assert result == rows

    def test_redact_single_column(self):
        rows = [
            {"id": 1, "name": "Alice", "ssn": "123-45-6789"},
            {"id": 2, "name": "Bob", "ssn": "987-65-4321"},
        ]
        result = redact_columns(rows, ["ssn"])
        assert result[0]["ssn"] == "***REDACTED***"
        assert result[1]["ssn"] == "***REDACTED***"
        assert result[0]["name"] == "Alice"

    def test_redact_multiple_columns(self):
        rows = [{"id": 1, "ssn": "123", "password": "secret", "name": "Alice"}]
        result = redact_columns(rows, ["ssn", "password"])
        assert result[0]["ssn"] == "***REDACTED***"
        assert result[0]["password"] == "***REDACTED***"
        assert result[0]["name"] == "Alice"
        assert result[0]["id"] == 1


# ═══════════════════════════════════════════════════════════════════════
# filter_table_list tests
# ═══════════════════════════════════════════════════════════════════════


@dataclass
class FakeTable:
    name: str


class TestFilterTableList:
    def test_no_filtering(self):
        guard = QueryGuard(DatabasePolicy())
        tables = [FakeTable("users"), FakeTable("orders")]
        result = filter_table_list(tables, guard)
        assert len(result) == 2

    def test_whitelist_filtering(self):
        guard = QueryGuard(DatabasePolicy(table_whitelist=["users"]))
        tables = [FakeTable("users"), FakeTable("secrets"), FakeTable("orders")]
        result = filter_table_list(tables, guard)
        assert len(result) == 1
        assert result[0].name == "users"

    def test_blacklist_filtering(self):
        guard = QueryGuard(DatabasePolicy(table_blacklist=["secrets", "audit_*"]))
        tables = [FakeTable("users"), FakeTable("secrets"), FakeTable("audit_log")]
        result = filter_table_list(tables, guard)
        assert len(result) == 1
        assert result[0].name == "users"


# ═══════════════════════════════════════════════════════════════════════
# SQL parsing helper tests
# ═══════════════════════════════════════════════════════════════════════


class TestSQLHelpers:
    def test_extract_select(self):
        assert _extract_statement_type("SELECT * FROM users") == "SELECT"

    def test_extract_insert(self):
        assert _extract_statement_type("INSERT INTO users VALUES (1)") == "INSERT"

    def test_extract_with_comment(self):
        assert _extract_statement_type("-- comment\nSELECT 1") == "SELECT"

    def test_extract_with_block_comment(self):
        assert _extract_statement_type("/* block */\nDROP TABLE x") == "DROP"

    def test_extract_with_whitespace(self):
        assert _extract_statement_type("   \n  UPDATE users SET x=1") == "UPDATE"

    def test_extract_unknown(self):
        assert _extract_statement_type("") == "UNKNOWN"

    def test_extract_table_from_select(self):
        tables = _extract_table_names("SELECT * FROM users")
        assert "users" in tables

    def test_extract_table_from_join(self):
        tables = _extract_table_names("SELECT * FROM users JOIN orders ON users.id = orders.uid")
        assert "users" in tables
        assert "orders" in tables

    def test_extract_table_from_insert(self):
        tables = _extract_table_names("INSERT INTO users VALUES (1)")
        assert "users" in tables

    def test_sanitize_long_query(self):
        q = "x" * 300
        assert len(_sanitize_query(q)) == 203
        assert _sanitize_query(q).endswith("...")

    def test_sanitize_whitespace(self):
        assert _sanitize_query("SELECT   *   FROM   users") == "SELECT * FROM users"


# ═══════════════════════════════════════════════════════════════════════
# _resolve_policy tests
# ═══════════════════════════════════════════════════════════════════════


class TestResolvePolicy:
    def test_readonly_preset(self):
        from digitorn.modules.database.module import _resolve_policy
        p = _resolve_policy({"preset": "readonly"})
        assert p.read_only is True

    def test_safe_write_preset(self):
        from digitorn.modules.database.module import _resolve_policy
        p = _resolve_policy({"preset": "safe_write"})
        assert "DROP" in p.blocked_statements

    def test_unrestricted_preset(self):
        from digitorn.modules.database.module import _resolve_policy
        p = _resolve_policy({"preset": "unrestricted"})
        assert p.read_only is False
        assert p.blocked_statements == []

    def test_unknown_preset_raises(self):
        from digitorn.modules.database.module import _resolve_policy
        with pytest.raises(ValueError, match="Unknown policy preset"):
            _resolve_policy({"preset": "nonexistent"})

    def test_custom_policy_dict(self):
        from digitorn.modules.database.module import _resolve_policy
        p = _resolve_policy({
            "read_only": True,
            "table_blacklist": ["secrets"],
            "max_rows_returned": 500,
        })
        assert p.read_only is True
        assert p.table_blacklist == ["secrets"]
        assert p.max_rows_returned == 500


# ═══════════════════════════════════════════════════════════════════════
# Module integration tests - security enforcement end-to-end
# ═══════════════════════════════════════════════════════════════════════


@dataclass
class FakeColumn:
    name: str
    type: str = "TEXT"
    nullable: bool = True
    primary_key: bool = False
    default: str | None = None
    comment: str | None = None


@dataclass
class FakeForeignKey:
    column: str
    referred_table: str
    referred_column: str


@dataclass
class FakeIndex:
    name: str
    columns: list[str]
    unique: bool = False


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
            self.columns = []
        if self.foreign_keys is None:
            self.foreign_keys = []
        if self.indexes is None:
            self.indexes = []


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


@dataclass
class FakeTableStats:
    row_count: int = 0
    size_bytes: int | None = None


class TestModuleSecurityIntegration:
    """Test that module actions correctly enforce QueryGuard policies."""

    @pytest.fixture
    def module(self):
        from digitorn.modules.database.module import DatabaseModule
        return DatabaseModule()

    @pytest.fixture
    def mock_adapter(self):
        _tables = [
            FakeTableInfo(
                name="users",
                columns=[FakeColumn("id", "INTEGER"), FakeColumn("name", "TEXT"), FakeColumn("ssn", "TEXT")],
            ),
            FakeTableInfo(name="secrets", columns=[FakeColumn("id", "INTEGER")]),
        ]
        adapter = AsyncMock()
        adapter.list_tables = AsyncMock(return_value=_tables)
        adapter.get_columns = AsyncMock(return_value=[
            FakeColumn("id", "INTEGER"), FakeColumn("name", "TEXT"), FakeColumn("ssn", "TEXT"),
        ])
        # introspect returns a schema-like object
        _schema = MagicMock()
        _schema.tables = _tables
        _schema.database = "test_db"
        _schema.driver = "sqlite"
        adapter.introspect = AsyncMock(return_value=_schema)
        adapter.execute = AsyncMock(return_value=FakeExecuteResult(rows_affected=1))
        adapter.fetch = AsyncMock(return_value=FakeFetchResult(
            columns=["id", "name", "ssn"],
            rows=[{"id": 1, "name": "Alice", "ssn": "123-45-6789"}],
            total_count=1,
        ))
        adapter.sample = AsyncMock(return_value=FakeFetchResult(
            columns=["id", "name", "ssn"],
            rows=[{"id": 1, "name": "Alice", "ssn": "123-45-6789"}],
            total_count=1,
        ))
        adapter.table_stats = AsyncMock(return_value=FakeTableStats(row_count=100))
        adapter.begin = AsyncMock()
        adapter.commit = AsyncMock()
        adapter.rollback = AsyncMock()
        return adapter

    def _setup_connection(self, module, mock_adapter, policy=None):
        """Register a mock connection with optional policy."""
        from digitorn.modules.database.connections import ConnectionEntry

        entry = ConnectionEntry(
            connection_id="test",
            driver="sqlite",
            url_display="sqlite:///:memory:",
            adapter=mock_adapter,
        )
        if policy:
            entry.guard = QueryGuard(policy)
        module.pool._connections["test"] = entry
        return entry

    @pytest.mark.asyncio
    async def test_execute_query_blocked_readonly(self, module, mock_adapter):
        from digitorn.modules.database.params import ExecuteQueryParams
        self._setup_connection(module, mock_adapter, DatabasePolicy(read_only=True))

        result = await module.execute_query(ExecuteQueryParams(
            connection_id="test", query="INSERT INTO users VALUES (1, 'x', '000')",
        ))
        assert result.success is False
        assert "Blocked by policy" in result.error
        assert "read_only" in result.error

    @pytest.mark.asyncio
    async def test_execute_query_allowed(self, module, mock_adapter):
        from digitorn.modules.database.params import ExecuteQueryParams
        self._setup_connection(module, mock_adapter, DatabasePolicy(read_only=True))

        # SELECT should be allowed even on read_only
        mock_adapter.execute = AsyncMock(return_value=FakeExecuteResult())
        result = await module.execute_query(ExecuteQueryParams(
            connection_id="test", query="SELECT 1",
        ))
        assert result.success is True

    @pytest.mark.asyncio
    async def test_fetch_results_column_redaction(self, module, mock_adapter):
        from digitorn.modules.database.params import FetchResultsParams
        self._setup_connection(module, mock_adapter, DatabasePolicy(
            column_blacklist={"users": ["ssn"]},
        ))

        result = await module.fetch_results(FetchResultsParams(
            connection_id="test", query="SELECT * FROM users",
        ))
        assert result.success is True
        assert result.data["rows"][0]["ssn"] == "***REDACTED***"
        assert result.data["rows"][0]["name"] == "Alice"

    @pytest.mark.asyncio
    async def test_fetch_results_row_limit(self, module, mock_adapter):
        from digitorn.modules.database.params import FetchResultsParams
        self._setup_connection(module, mock_adapter, DatabasePolicy(max_rows_returned=5))

        await module.fetch_results(FetchResultsParams(
            connection_id="test", query="SELECT * FROM users", limit=1000,
        ))
        # The adapter should have been called with the policy limit
        call_args = mock_adapter.fetch.call_args
        assert call_args[0][2] == 5  # limit arg

    @pytest.mark.asyncio
    async def test_list_tables_filtered(self, module, mock_adapter):
        from digitorn.modules.database.params import ListTablesParams
        self._setup_connection(module, mock_adapter, DatabasePolicy(
            table_blacklist=["secrets"],
        ))

        result = await module.list_tables(ListTablesParams(connection_id="test"))
        assert result.success is True
        table_names = [t["name"] for t in result.data["tables"]]
        assert "users" in table_names
        assert "secrets" not in table_names

    @pytest.mark.asyncio
    async def test_get_table_schema_blocked(self, module, mock_adapter):
        from digitorn.modules.database.params import GetTableSchemaParams
        self._setup_connection(module, mock_adapter, DatabasePolicy(
            table_blacklist=["secrets"],
        ))

        result = await module.get_table_schema(GetTableSchemaParams(
            connection_id="test", table="secrets",
        ))
        assert result.success is False
        assert "Blocked by policy" in result.error

    @pytest.mark.asyncio
    async def test_table_stats_blocked(self, module, mock_adapter):
        from digitorn.modules.database.params import TableStatsParams
        self._setup_connection(module, mock_adapter, DatabasePolicy(
            table_whitelist=["users"],
        ))

        result = await module.table_stats(TableStatsParams(
            connection_id="test", table="secrets",
        ))
        assert result.success is False
        assert "Blocked by policy" in result.error

    @pytest.mark.asyncio
    async def test_sample_blocked_table(self, module, mock_adapter):
        from digitorn.modules.database.params import SampleParams
        self._setup_connection(module, mock_adapter, DatabasePolicy(
            table_blacklist=["secrets"],
        ))

        result = await module.sample(SampleParams(
            connection_id="test", table="secrets",
        ))
        assert result.success is False
        assert "Blocked by policy" in result.error

    @pytest.mark.asyncio
    async def test_sample_column_redaction(self, module, mock_adapter):
        from digitorn.modules.database.params import SampleParams
        self._setup_connection(module, mock_adapter, DatabasePolicy(
            column_blacklist={"users": ["ssn"]},
        ))

        result = await module.sample(SampleParams(
            connection_id="test", table="users",
        ))
        assert result.success is True
        assert result.data["rows"][0]["ssn"] == "***REDACTED***"

    @pytest.mark.asyncio
    async def test_describe_blocked_table(self, module, mock_adapter):
        from digitorn.modules.database.params import DescribeParams
        self._setup_connection(module, mock_adapter, DatabasePolicy(
            table_blacklist=["secrets"],
        ))

        result = await module.describe(DescribeParams(
            connection_id="test", table="secrets",
        ))
        assert result.success is False
        assert "Blocked by policy" in result.error

    @pytest.mark.asyncio
    async def test_describe_column_redaction(self, module, mock_adapter):
        from digitorn.modules.database.params import DescribeParams
        self._setup_connection(module, mock_adapter, DatabasePolicy(
            column_blacklist={"users": ["ssn"]},
        ))

        result = await module.describe(DescribeParams(
            connection_id="test", table="users",
        ))
        assert result.success is True
        assert result.data["sample"]["rows"][0]["ssn"] == "***REDACTED***"
        assert result.data["sample"]["rows"][0]["name"] == "Alice"

    @pytest.mark.asyncio
    async def test_begin_transaction_blocked(self, module, mock_adapter):
        from digitorn.modules.database.params import BeginTransactionParams
        self._setup_connection(module, mock_adapter, DatabasePolicy(
            allow_transactions=False,
        ))

        result = await module.begin_transaction(BeginTransactionParams(
            connection_id="test",
        ))
        assert result.success is False
        assert "Blocked by policy" in result.error
        assert "allow_transactions" in result.error

    @pytest.mark.asyncio
    async def test_commit_transaction_blocked(self, module, mock_adapter):
        from digitorn.modules.database.params import CommitTransactionParams
        self._setup_connection(module, mock_adapter, DatabasePolicy(
            allow_transactions=False,
        ))

        result = await module.commit_transaction(CommitTransactionParams(
            connection_id="test",
        ))
        assert result.success is False
        assert "Blocked by policy" in result.error

    @pytest.mark.asyncio
    async def test_rollback_transaction_blocked(self, module, mock_adapter):
        from digitorn.modules.database.params import RollbackTransactionParams
        self._setup_connection(module, mock_adapter, DatabasePolicy(
            allow_transactions=False,
        ))

        result = await module.rollback_transaction(RollbackTransactionParams(
            connection_id="test",
        ))
        assert result.success is False
        assert "Blocked by policy" in result.error

    @pytest.mark.asyncio
    async def test_transaction_allowed(self, module, mock_adapter):
        from digitorn.modules.database.params import BeginTransactionParams
        self._setup_connection(module, mock_adapter, DatabasePolicy(
            allow_transactions=True,
        ))

        result = await module.begin_transaction(BeginTransactionParams(
            connection_id="test",
        ))
        assert result.success is True

    @pytest.mark.asyncio
    async def test_introspect_filtered(self, module, mock_adapter):
        from digitorn.modules.database.params import IntrospectParams

        # introspect returns a schema object, not a list
        schema_obj = MagicMock()
        schema_obj.database = "test_db"
        schema_obj.driver = "sqlite"
        schema_obj.tables = [
            FakeTableInfo(
                name="users",
                columns=[FakeColumn("id", "INTEGER")],
            ),
            FakeTableInfo(name="secrets", columns=[FakeColumn("id", "INTEGER")]),
        ]
        mock_adapter.introspect = AsyncMock(return_value=schema_obj)
        self._setup_connection(module, mock_adapter, DatabasePolicy(
            table_blacklist=["secrets"],
        ))

        result = await module.introspect(IntrospectParams(connection_id="test"))
        assert result.success is True
        table_names = [t["name"] for t in result.data["tables"]]
        assert "users" in table_names
        assert "secrets" not in table_names

    @pytest.mark.asyncio
    async def test_set_policy_action(self, module, mock_adapter):
        from digitorn.modules.database.params import SetPolicyParams
        self._setup_connection(module, mock_adapter)

        result = await module.set_policy(SetPolicyParams(
            connection_id="test",
            policy={"read_only": True, "table_blacklist": ["secrets"]},
        ))
        assert result.success is True
        assert result.data["policy"]["read_only"] is True
        assert "secrets" in result.data["policy"]["table_blacklist"]

        # Verify the guard is now active
        guard = module._get_guard("test")
        assert guard is not None
        assert guard.policy.read_only is True

    @pytest.mark.asyncio
    async def test_set_policy_preset(self, module, mock_adapter):
        from digitorn.modules.database.params import SetPolicyParams
        self._setup_connection(module, mock_adapter)

        result = await module.set_policy(SetPolicyParams(
            connection_id="test",
            policy={"preset": "readonly"},
        ))
        assert result.success is True
        assert result.data["policy"]["read_only"] is True

    @pytest.mark.asyncio
    async def test_set_policy_merges_existing(self, module, mock_adapter):
        from digitorn.modules.database.params import SetPolicyParams
        self._setup_connection(module, mock_adapter, DatabasePolicy(
            read_only=True, table_blacklist=["secrets"],
        ))

        result = await module.set_policy(SetPolicyParams(
            connection_id="test",
            policy={"max_rows_returned": 500},
        ))
        assert result.success is True
        # Should keep existing read_only + blacklist AND add new row limit
        assert result.data["policy"]["read_only"] is True
        assert result.data["policy"]["max_rows_returned"] == 500
        assert "secrets" in result.data["policy"]["table_blacklist"]

    @pytest.mark.asyncio
    async def test_set_policy_unknown_connection(self, module, mock_adapter):
        from digitorn.modules.database.params import SetPolicyParams

        result = await module.set_policy(SetPolicyParams(
            connection_id="nonexistent",
            policy={"read_only": True},
        ))
        assert result.success is False

    @pytest.mark.asyncio
    async def test_get_audit_log(self, module, mock_adapter):
        from digitorn.modules.database.params import GetAuditLogParams

        # Generate some audit entries
        module.audit.log(connection_id="main", action="fetch_results", driver="sqlite")
        module.audit.log(connection_id="main", action="execute_query", driver="sqlite", blocked=True, block_reason="read_only")

        result = await module.get_audit_log(GetAuditLogParams(limit=10))
        assert result.success is True
        assert result.data["count"] == 2
        assert result.data["stats"]["total_operations"] == 2
        assert result.data["stats"]["blocked_operations"] == 1

    @pytest.mark.asyncio
    async def test_audit_logged_on_execute(self, module, mock_adapter):
        from digitorn.modules.database.params import ExecuteQueryParams
        self._setup_connection(module, mock_adapter)

        await module.execute_query(ExecuteQueryParams(
            connection_id="test", query="INSERT INTO users VALUES (1, 'x', '000')",
        ))
        entries = module.audit.get_recent(10)
        assert len(entries) == 1
        assert entries[0]["action"] == "execute_query"
        assert entries[0]["blocked"] is False

    @pytest.mark.asyncio
    async def test_audit_logged_on_blocked_query(self, module, mock_adapter):
        from digitorn.modules.database.params import ExecuteQueryParams
        self._setup_connection(module, mock_adapter, DatabasePolicy(read_only=True))

        await module.execute_query(ExecuteQueryParams(
            connection_id="test", query="INSERT INTO users VALUES (1, 'x', '000')",
        ))
        entries = module.audit.get_recent(10)
        assert len(entries) == 1
        assert entries[0]["blocked"] is True
        assert entries[0]["block_reason"] != ""

    @pytest.mark.asyncio
    async def test_no_policy_no_restrictions(self, module, mock_adapter):
        """Connections without policies should have no restrictions."""
        from digitorn.modules.database.params import ExecuteQueryParams
        self._setup_connection(module, mock_adapter)  # No policy

        result = await module.execute_query(ExecuteQueryParams(
            connection_id="test", query="DROP TABLE users",
        ))
        assert result.success is True


# ═══════════════════════════════════════════════════════════════════════
# Query timeout tests
# ═══════════════════════════════════════════════════════════════════════


class TestQueryTimeout:
    """Test that max_query_time_seconds is enforced via asyncio.wait_for."""

    @pytest.fixture
    def module(self):
        from digitorn.modules.database.module import DatabaseModule
        return DatabaseModule()

    def _setup_connection(self, module, adapter, policy=None):
        from digitorn.modules.database.connections import ConnectionEntry

        entry = ConnectionEntry(
            connection_id="test",
            driver="sqlite",
            url_display="sqlite:///:memory:",
            adapter=adapter,
        )
        if policy:
            entry.guard = QueryGuard(policy)
        module.pool._connections["test"] = entry
        return entry

    @pytest.mark.asyncio
    async def test_execute_query_timeout(self, module):
        """execute_query should return error on timeout."""
        from digitorn.modules.database.params import ExecuteQueryParams

        async def slow_execute(*args, **kwargs):
            await asyncio.sleep(10)

        adapter = AsyncMock()
        adapter.execute = slow_execute
        self._setup_connection(module, adapter, DatabasePolicy(max_query_time_seconds=0.1))

        result = await module.execute_query(ExecuteQueryParams(
            connection_id="test", query="SELECT pg_sleep(100)",
        ))
        assert result.success is False
        assert "timed out" in result.error
        assert "max_query_time_seconds" in result.error

    @pytest.mark.asyncio
    async def test_fetch_results_timeout(self, module):
        """fetch_results should return error on timeout."""
        from digitorn.modules.database.params import FetchResultsParams

        async def slow_fetch(*args, **kwargs):
            await asyncio.sleep(10)

        adapter = AsyncMock()
        adapter.fetch = slow_fetch
        self._setup_connection(module, adapter, DatabasePolicy(max_query_time_seconds=0.1))

        result = await module.fetch_results(FetchResultsParams(
            connection_id="test", query="SELECT * FROM huge_table",
        ))
        assert result.success is False
        assert "timed out" in result.error

    @pytest.mark.asyncio
    async def test_timeout_is_audited(self, module):
        """Timeout should appear in audit log as blocked."""
        from digitorn.modules.database.params import ExecuteQueryParams

        async def slow_execute(*args, **kwargs):
            await asyncio.sleep(10)

        adapter = AsyncMock()
        adapter.execute = slow_execute
        self._setup_connection(module, adapter, DatabasePolicy(max_query_time_seconds=0.1))

        await module.execute_query(ExecuteQueryParams(
            connection_id="test", query="SELECT pg_sleep(100)",
        ))
        entries = module.audit.get_recent(10)
        assert len(entries) == 1
        assert entries[0]["blocked"] is True
        assert entries[0]["block_reason"] == "query_timeout"

    @pytest.mark.asyncio
    async def test_no_timeout_without_policy(self, module):
        """Without policy, no timeout is applied."""
        from digitorn.modules.database.params import ExecuteQueryParams

        adapter = AsyncMock()
        adapter.execute = AsyncMock(return_value=FakeExecuteResult(rows_affected=1))
        self._setup_connection(module, adapter)  # No policy

        result = await module.execute_query(ExecuteQueryParams(
            connection_id="test", query="INSERT INTO t VALUES (1)",
        ))
        assert result.success is True

    @pytest.mark.asyncio
    async def test_fast_query_succeeds_with_timeout(self, module):
        """Fast queries should succeed even with tight timeout."""
        from digitorn.modules.database.params import ExecuteQueryParams

        adapter = AsyncMock()
        adapter.execute = AsyncMock(return_value=FakeExecuteResult(rows_affected=1))
        self._setup_connection(module, adapter, DatabasePolicy(max_query_time_seconds=5))

        result = await module.execute_query(ExecuteQueryParams(
            connection_id="test", query="INSERT INTO t VALUES (1)",
        ))
        assert result.success is True


# ═══════════════════════════════════════════════════════════════════════
# Transaction timeout tests
# ═══════════════════════════════════════════════════════════════════════


class TestTransactionTimeout:
    """Test that max_transaction_seconds is enforced."""

    @pytest.fixture
    def module(self):
        from digitorn.modules.database.module import DatabaseModule
        return DatabaseModule()

    def _setup_connection(self, module, adapter, policy=None):
        from digitorn.modules.database.connections import ConnectionEntry

        entry = ConnectionEntry(
            connection_id="test",
            driver="sqlite",
            url_display="sqlite:///:memory:",
            adapter=adapter,
        )
        if policy:
            entry.guard = QueryGuard(policy)
        module.pool._connections["test"] = entry
        return entry

    @pytest.mark.asyncio
    async def test_begin_records_start_time(self, module):
        """begin_transaction should record the start time."""
        import time as _time
        from digitorn.modules.database.params import BeginTransactionParams

        adapter = AsyncMock()
        self._setup_connection(module, adapter, DatabasePolicy(max_transaction_seconds=60))

        before = _time.monotonic()
        result = await module.begin_transaction(BeginTransactionParams(connection_id="test"))
        assert result.success is True
        assert "test" in module._tx_start_times
        assert module._tx_start_times["test"] >= before
        assert "Timeout: 60s" in result.data["message"]

    @pytest.mark.asyncio
    async def test_commit_clears_start_time(self, module):
        """commit_transaction should clear the start time."""
        from digitorn.modules.database.params import BeginTransactionParams, CommitTransactionParams

        adapter = AsyncMock()
        self._setup_connection(module, adapter, DatabasePolicy())

        await module.begin_transaction(BeginTransactionParams(connection_id="test"))
        assert "test" in module._tx_start_times

        await module.commit_transaction(CommitTransactionParams(connection_id="test"))
        assert "test" not in module._tx_start_times

    @pytest.mark.asyncio
    async def test_rollback_clears_start_time(self, module):
        """rollback_transaction should clear the start time."""
        from digitorn.modules.database.params import BeginTransactionParams, RollbackTransactionParams

        adapter = AsyncMock()
        self._setup_connection(module, adapter, DatabasePolicy())

        await module.begin_transaction(BeginTransactionParams(connection_id="test"))
        assert "test" in module._tx_start_times

        await module.rollback_transaction(RollbackTransactionParams(connection_id="test"))
        assert "test" not in module._tx_start_times

    @pytest.mark.asyncio
    async def test_transaction_timeout_blocks_execute(self, module):
        """execute_query should fail if transaction exceeded timeout."""
        import time as _time
        from digitorn.modules.database.params import ExecuteQueryParams

        adapter = AsyncMock()
        adapter.execute = AsyncMock(return_value=FakeExecuteResult(rows_affected=1))
        self._setup_connection(module, adapter, DatabasePolicy(max_transaction_seconds=1))

        # Simulate a transaction that started 2 seconds ago
        module._tx_start_times["test"] = _time.monotonic() - 2

        result = await module.execute_query(ExecuteQueryParams(
            connection_id="test", query="INSERT INTO t VALUES (1)",
        ))
        assert result.success is False
        assert "max_transaction_seconds" in result.error

    @pytest.mark.asyncio
    async def test_transaction_timeout_blocks_fetch(self, module):
        """fetch_results should fail if transaction exceeded timeout."""
        import time as _time
        from digitorn.modules.database.params import FetchResultsParams

        adapter = AsyncMock()
        adapter.fetch = AsyncMock(return_value=FakeFetchResult(
            columns=["id"], rows=[{"id": 1}], total_count=1,
        ))
        self._setup_connection(module, adapter, DatabasePolicy(max_transaction_seconds=1))

        module._tx_start_times["test"] = _time.monotonic() - 2

        result = await module.fetch_results(FetchResultsParams(
            connection_id="test", query="SELECT * FROM t",
        ))
        assert result.success is False
        assert "max_transaction_seconds" in result.error

    @pytest.mark.asyncio
    async def test_transaction_timeout_blocks_commit(self, module):
        """commit_transaction should fail if transaction exceeded timeout."""
        import time as _time
        from digitorn.modules.database.params import CommitTransactionParams

        adapter = AsyncMock()
        self._setup_connection(module, adapter, DatabasePolicy(max_transaction_seconds=1))

        module._tx_start_times["test"] = _time.monotonic() - 2

        result = await module.commit_transaction(CommitTransactionParams(connection_id="test"))
        assert result.success is False
        assert "max_transaction_seconds" in result.error

    @pytest.mark.asyncio
    async def test_rollback_always_allowed_after_timeout(self, module):
        """rollback should succeed even after transaction timeout (to allow cleanup)."""
        import time as _time
        from digitorn.modules.database.params import RollbackTransactionParams

        adapter = AsyncMock()
        self._setup_connection(module, adapter, DatabasePolicy(max_transaction_seconds=1))

        module._tx_start_times["test"] = _time.monotonic() - 2

        result = await module.rollback_transaction(RollbackTransactionParams(connection_id="test"))
        assert result.success is True
        assert "test" not in module._tx_start_times

    @pytest.mark.asyncio
    async def test_no_timeout_without_transaction(self, module):
        """Queries outside a transaction should not be affected by transaction timeout."""
        from digitorn.modules.database.params import ExecuteQueryParams

        adapter = AsyncMock()
        adapter.execute = AsyncMock(return_value=FakeExecuteResult(rows_affected=1))
        self._setup_connection(module, adapter, DatabasePolicy(max_transaction_seconds=1))

        # No transaction started - no entry in _tx_start_times
        result = await module.execute_query(ExecuteQueryParams(
            connection_id="test", query="INSERT INTO t VALUES (1)",
        ))
        assert result.success is True


# ═══════════════════════════════════════════════════════════════════════
# Introspection audit logging tests
# ═══════════════════════════════════════════════════════════════════════


class TestIntrospectionAudit:
    """Test that introspection actions are recorded in the audit log."""

    @pytest.fixture
    def module(self):
        from digitorn.modules.database.module import DatabaseModule
        return DatabaseModule()

    @pytest.fixture
    def mock_adapter(self):
        adapter = AsyncMock()
        adapter.list_tables = AsyncMock(return_value=[
            FakeTableInfo(name="users", columns=[FakeColumn("id", "INTEGER")]),
        ])
        adapter.get_columns = AsyncMock(return_value=[
            FakeColumn("id", "INTEGER"), FakeColumn("name", "TEXT"),
        ])
        adapter.table_stats = AsyncMock(return_value=FakeTableStats(row_count=42))
        adapter.sample = AsyncMock(return_value=FakeFetchResult(
            columns=["id", "name"], rows=[{"id": 1, "name": "Alice"}], total_count=1,
        ))
        schema_obj = MagicMock()
        schema_obj.database = "test_db"
        schema_obj.driver = "sqlite"
        schema_obj.tables = [FakeTableInfo(name="users", columns=[FakeColumn("id", "INTEGER")])]
        adapter.introspect = AsyncMock(return_value=schema_obj)
        return adapter

    def _setup_connection(self, module, adapter, policy=None):
        from digitorn.modules.database.connections import ConnectionEntry

        entry = ConnectionEntry(
            connection_id="test",
            driver="sqlite",
            url_display="sqlite:///:memory:",
            adapter=adapter,
        )
        if policy:
            entry.guard = QueryGuard(policy)
        module.pool._connections["test"] = entry

    @pytest.mark.asyncio
    async def test_list_tables_audited(self, module, mock_adapter):
        from digitorn.modules.database.params import ListTablesParams
        self._setup_connection(module, mock_adapter)

        await module.list_tables(ListTablesParams(connection_id="test"))
        entries = module.audit.get_recent(10)
        assert len(entries) == 1
        assert entries[0]["action"] == "list_tables"

    @pytest.mark.asyncio
    async def test_get_table_schema_audited(self, module, mock_adapter):
        from digitorn.modules.database.params import GetTableSchemaParams
        self._setup_connection(module, mock_adapter)

        await module.get_table_schema(GetTableSchemaParams(
            connection_id="test", table="users",
        ))
        entries = module.audit.get_recent(10)
        assert len(entries) == 1
        assert entries[0]["action"] == "get_table_schema"
        assert entries[0]["tables_affected"] == ["users"]

    @pytest.mark.asyncio
    async def test_get_table_schema_blocked_audited(self, module, mock_adapter):
        from digitorn.modules.database.params import GetTableSchemaParams
        self._setup_connection(module, mock_adapter, DatabasePolicy(
            table_blacklist=["secrets"],
        ))

        result = await module.get_table_schema(GetTableSchemaParams(
            connection_id="test", table="secrets",
        ))
        assert result.success is False
        entries = module.audit.get_recent(10)
        assert len(entries) == 1
        assert entries[0]["blocked"] is True

    @pytest.mark.asyncio
    async def test_table_stats_audited(self, module, mock_adapter):
        from digitorn.modules.database.params import TableStatsParams
        self._setup_connection(module, mock_adapter)

        await module.table_stats(TableStatsParams(
            connection_id="test", table="users",
        ))
        entries = module.audit.get_recent(10)
        assert len(entries) == 1
        assert entries[0]["action"] == "table_stats"

    @pytest.mark.asyncio
    async def test_sample_audited(self, module, mock_adapter):
        from digitorn.modules.database.params import SampleParams
        self._setup_connection(module, mock_adapter)

        await module.sample(SampleParams(connection_id="test", table="users"))
        entries = module.audit.get_recent(10)
        assert len(entries) == 1
        assert entries[0]["action"] == "sample"
        assert entries[0]["tables_affected"] == ["users"]

    @pytest.mark.asyncio
    async def test_sample_blocked_audited(self, module, mock_adapter):
        from digitorn.modules.database.params import SampleParams
        self._setup_connection(module, mock_adapter, DatabasePolicy(
            table_blacklist=["secrets"],
        ))

        result = await module.sample(SampleParams(connection_id="test", table="secrets"))
        assert result.success is False
        entries = module.audit.get_recent(10)
        assert len(entries) == 1
        assert entries[0]["blocked"] is True

    @pytest.mark.asyncio
    async def test_introspect_audited(self, module, mock_adapter):
        from digitorn.modules.database.params import IntrospectParams
        self._setup_connection(module, mock_adapter)

        await module.introspect(IntrospectParams(connection_id="test"))
        entries = module.audit.get_recent(10)
        assert len(entries) == 1
        assert entries[0]["action"] == "introspect"

    @pytest.mark.asyncio
    async def test_describe_audited(self, module, mock_adapter):
        from digitorn.modules.database.params import DescribeParams
        self._setup_connection(module, mock_adapter)

        await module.describe(DescribeParams(connection_id="test", table="users"))
        entries = module.audit.get_recent(10)
        assert len(entries) == 1
        assert entries[0]["action"] == "describe"
        assert entries[0]["tables_affected"] == ["users"]

    @pytest.mark.asyncio
    async def test_describe_blocked_audited(self, module, mock_adapter):
        from digitorn.modules.database.params import DescribeParams
        self._setup_connection(module, mock_adapter, DatabasePolicy(
            table_blacklist=["secrets"],
        ))

        result = await module.describe(DescribeParams(
            connection_id="test", table="secrets",
        ))
        assert result.success is False
        entries = module.audit.get_recent(10)
        assert len(entries) == 1
        assert entries[0]["blocked"] is True


# ═══════════════════════════════════════════════════════════════════════
# QueryGuard helper method tests
# ═══════════════════════════════════════════════════════════════════════


class TestQueryGuardHelpers:
    def test_get_query_timeout(self):
        guard = QueryGuard(DatabasePolicy(max_query_time_seconds=15.0))
        assert guard.get_query_timeout() == 15.0

    def test_get_transaction_timeout(self):
        guard = QueryGuard(DatabasePolicy(max_transaction_seconds=120.0))
        assert guard.get_transaction_timeout() == 120.0

    def test_default_timeouts(self):
        guard = QueryGuard(DatabasePolicy())
        assert guard.get_query_timeout() == 30.0
        assert guard.get_transaction_timeout() == 300.0
