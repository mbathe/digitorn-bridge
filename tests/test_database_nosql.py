"""Tests for NoSQL database adapters — MongoDB (MongoAdapter) and Redis (RedisAdapter).

Uses mocks since these adapters require external servers.
Also tests URL builders, helpers, and ConnectionPool integration.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from digitorn.modules.database.adapters.base import (
    ColumnInfo,
    DatabaseAdapter,
    ExecuteResult,
    FetchResult,
    IndexInfo,
    ItemChecksum,
    SchemaInfo,
    TableInfo,
    TableStats,
    WatchItem,
)
from digitorn.modules.database.connections import (
    ConnectionPool,
    _build_mongo_url,
    _build_redis_url,
)


# ═══════════════════════════════════════════════════════════════════════
# URL Builder Tests
# ═══════════════════════════════════════════════════════════════════════


class TestMongoURLBuilder:

    def test_basic_url(self):
        url = _build_mongo_url("mydb", "dbhost", 27017, None, None)
        assert url == "mongodb://dbhost:27017/mydb"

    def test_url_with_auth(self):
        url = _build_mongo_url("mydb", "dbhost", 27017, "admin", "secret")
        assert url == "mongodb://admin:secret@dbhost:27017/mydb"

    def test_url_defaults(self):
        url = _build_mongo_url(None, None, None, None, None)
        assert url == "mongodb://localhost:27017/test"

    def test_url_no_port(self):
        url = _build_mongo_url("mydb", "host", None, None, None)
        assert url == "mongodb://host:27017/mydb"

    def test_url_with_username_only(self):
        url = _build_mongo_url("mydb", "host", 27017, "admin", None)
        assert url == "mongodb://admin@host:27017/mydb"

    def test_build_url_via_pool(self):
        url = ConnectionPool._build_url("mongodb", "mydb", "host", 27017, "u", "p", allowed_hosts=["host"])
        assert url == "mongodb://u:p@host:27017/mydb"


class TestRedisURLBuilder:

    def test_basic_url(self):
        url = _build_redis_url("localhost", 6379, None, "0")
        assert url == "redis://localhost:6379/0"

    def test_url_with_password(self):
        url = _build_redis_url("localhost", 6379, "secret", "0")
        assert url == "redis://:secret@localhost:6379/0"

    def test_url_defaults(self):
        url = _build_redis_url(None, None, None, None)
        assert url == "redis://localhost:6379/0"

    def test_url_custom_db(self):
        url = _build_redis_url("host", 6380, None, "3")
        assert url == "redis://host:6380/3"

    def test_build_url_via_pool(self):
        url = ConnectionPool._build_url("redis", "2", "host", 6380, None, "pass", allowed_hosts=["host"])
        assert url == "redis://:pass@host:6380/2"


# ═══════════════════════════════════════════════════════════════════════
# MongoAdapter — Mocked Tests
# ═══════════════════════════════════════════════════════════════════════


class TestMongoAdapterMocked:
    """Test MongoAdapter with mocked motor client."""

    @pytest.fixture
    def adapter(self):
        from digitorn.modules.database.adapters.mongo import MongoAdapter
        a = MongoAdapter()
        # Mock the client and database
        a._client = MagicMock()
        a._db = MagicMock()
        a._db_name = "testdb"
        return a

    def test_properties(self, adapter):
        assert adapter.connected is True
        assert adapter.driver_name == "mongodb"

    def test_not_connected(self):
        from digitorn.modules.database.adapters.mongo import MongoAdapter
        a = MongoAdapter()
        assert a.connected is False
        assert a.driver_name == "mongodb"

    def test_not_connected_raises(self):
        from digitorn.modules.database.adapters.mongo import MongoAdapter
        a = MongoAdapter()
        with pytest.raises(RuntimeError, match="Not connected"):
            from _test_helpers import run_coro
            run_coro(
                a.execute('{"operation": "insert_one", "collection": "t", "document": {}}')
            )

    @pytest.mark.asyncio
    async def test_execute_insert_one(self, adapter):
        mock_coll = AsyncMock()
        mock_coll.insert_one.return_value = MagicMock(inserted_id="abc123")
        adapter._db.__getitem__ = MagicMock(return_value=mock_coll)

        result = await adapter.execute(json.dumps({
            "collection": "users",
            "operation": "insert_one",
            "document": {"name": "Alice", "age": 30},
        }))

        assert isinstance(result, ExecuteResult)
        assert result.rows_affected == 1
        assert result.last_insert_id == "abc123"
        mock_coll.insert_one.assert_called_once()

    @pytest.mark.asyncio
    async def test_execute_insert_many(self, adapter):
        mock_coll = AsyncMock()
        mock_coll.insert_many.return_value = MagicMock(inserted_ids=["a", "b", "c"])
        adapter._db.__getitem__ = MagicMock(return_value=mock_coll)

        result = await adapter.execute(json.dumps({
            "collection": "users",
            "operation": "insert_many",
            "documents": [{"name": "A"}, {"name": "B"}, {"name": "C"}],
        }))

        assert result.rows_affected == 3

    @pytest.mark.asyncio
    async def test_execute_update_one(self, adapter):
        mock_coll = AsyncMock()
        mock_coll.update_one.return_value = MagicMock(modified_count=1)
        adapter._db.__getitem__ = MagicMock(return_value=mock_coll)

        result = await adapter.execute(json.dumps({
            "collection": "users",
            "operation": "update_one",
            "filter": {"name": "Alice"},
            "update": {"$set": {"age": 31}},
        }))

        assert result.rows_affected == 1

    @pytest.mark.asyncio
    async def test_execute_update_many(self, adapter):
        mock_coll = AsyncMock()
        mock_coll.update_many.return_value = MagicMock(modified_count=5)
        adapter._db.__getitem__ = MagicMock(return_value=mock_coll)

        result = await adapter.execute(json.dumps({
            "collection": "users",
            "operation": "update_many",
            "filter": {"active": False},
            "update": {"$set": {"archived": True}},
        }))

        assert result.rows_affected == 5

    @pytest.mark.asyncio
    async def test_execute_delete_one(self, adapter):
        mock_coll = AsyncMock()
        mock_coll.delete_one.return_value = MagicMock(deleted_count=1)
        adapter._db.__getitem__ = MagicMock(return_value=mock_coll)

        result = await adapter.execute(json.dumps({
            "collection": "users",
            "operation": "delete_one",
            "filter": {"name": "Alice"},
        }))

        assert result.rows_affected == 1

    @pytest.mark.asyncio
    async def test_execute_delete_many(self, adapter):
        mock_coll = AsyncMock()
        mock_coll.delete_many.return_value = MagicMock(deleted_count=10)
        adapter._db.__getitem__ = MagicMock(return_value=mock_coll)

        result = await adapter.execute(json.dumps({
            "collection": "users",
            "operation": "delete_many",
            "filter": {"archived": True},
        }))

        assert result.rows_affected == 10

    @pytest.mark.asyncio
    async def test_execute_create_collection(self, adapter):
        adapter._db.create_collection = AsyncMock()

        result = await adapter.execute(json.dumps({
            "operation": "create_collection",
            "collection": "new_coll",
        }))

        assert result.rows_affected == 0
        adapter._db.create_collection.assert_called_once_with("new_coll")

    @pytest.mark.asyncio
    async def test_execute_drop_collection(self, adapter):
        mock_coll = AsyncMock()
        adapter._db.__getitem__ = MagicMock(return_value=mock_coll)

        result = await adapter.execute(json.dumps({
            "operation": "drop_collection",
            "collection": "old_coll",
        }))

        assert result.rows_affected == 0
        mock_coll.drop.assert_called_once()

    @pytest.mark.asyncio
    async def test_execute_unknown_operation(self, adapter):
        with pytest.raises(ValueError, match="Unknown operation"):
            await adapter.execute(json.dumps({
                "collection": "t",
                "operation": "invalid_op",
            }))

    @pytest.mark.asyncio
    async def test_fetch(self, adapter):
        # Create an async iterator mock for the cursor
        docs = [
            {"_id": "id1", "name": "Alice", "age": 30},
            {"_id": "id2", "name": "Bob", "age": 25},
        ]
        mock_cursor = AsyncIteratorMock(docs)
        mock_cursor.sort = MagicMock(return_value=mock_cursor)
        mock_cursor.limit = MagicMock(return_value=mock_cursor)

        mock_coll = MagicMock()
        mock_coll.find = MagicMock(return_value=mock_cursor)
        adapter._db.__getitem__ = MagicMock(return_value=mock_coll)

        result = await adapter.fetch(json.dumps({
            "collection": "users",
            "filter": {"age": {"$gt": 20}},
        }))

        assert isinstance(result, FetchResult)
        assert len(result.rows) == 2
        assert result.rows[0]["name"] == "Alice"
        assert "name" in result.columns

    @pytest.mark.asyncio
    async def test_fetch_with_sort(self, adapter):
        docs = [{"_id": "id1", "name": "Alice"}]
        mock_cursor = AsyncIteratorMock(docs)
        mock_cursor.sort = MagicMock(return_value=mock_cursor)
        mock_cursor.limit = MagicMock(return_value=mock_cursor)

        mock_coll = MagicMock()
        mock_coll.find = MagicMock(return_value=mock_cursor)
        adapter._db.__getitem__ = MagicMock(return_value=mock_coll)

        result = await adapter.fetch(json.dumps({
            "collection": "users",
            "filter": {},
            "sort": {"name": 1},
        }))

        mock_cursor.sort.assert_called_once_with([("name", 1)])
        assert len(result.rows) == 1

    @pytest.mark.asyncio
    async def test_table_stats(self, adapter):
        mock_coll = AsyncMock()
        mock_coll.estimated_document_count.return_value = 42
        adapter._db.__getitem__ = MagicMock(return_value=mock_coll)
        adapter._db.command = AsyncMock(return_value={"size": 1024})

        stats = await adapter.table_stats("users")

        assert isinstance(stats, TableStats)
        assert stats.name == "users"
        assert stats.row_count == 42
        assert stats.size_bytes == 1024

    @pytest.mark.asyncio
    async def test_list_tables(self, adapter):
        adapter._db.list_collection_names = AsyncMock(
            return_value=["users", "orders", "system.sessions"],
        )

        # Mock _infer_columns and _get_indexes
        async def mock_infer(name):
            return [
                ColumnInfo(name="_id", type="objectId", primary_key=True),
                ColumnInfo(name="name", type="string"),
            ]

        async def mock_indexes(name):
            return []

        adapter._infer_columns = mock_infer
        adapter._get_indexes = mock_indexes

        tables = await adapter.list_tables()

        assert len(tables) == 2  # system.sessions filtered out
        names = {t.name for t in tables}
        assert names == {"users", "orders"}
        assert all(isinstance(t, TableInfo) for t in tables)

    @pytest.mark.asyncio
    async def test_introspect(self, adapter):
        adapter._db.list_collection_names = AsyncMock(return_value=["users"])

        async def mock_infer(name):
            return [ColumnInfo(name="_id", type="objectId", primary_key=True)]

        async def mock_indexes(name):
            return []

        adapter._infer_columns = mock_infer
        adapter._get_indexes = mock_indexes

        schema = await adapter.introspect()

        assert isinstance(schema, SchemaInfo)
        assert schema.database == "testdb"
        assert schema.driver == "mongodb"
        assert len(schema.tables) == 1

    @pytest.mark.asyncio
    async def test_sample(self, adapter):
        docs = [
            {"_id": "id1", "name": "Alice"},
            {"_id": "id2", "name": "Bob"},
        ]
        mock_cursor = AsyncIteratorMock(docs)
        mock_cursor.limit = MagicMock(return_value=mock_cursor)

        mock_coll = MagicMock()
        mock_coll.find = MagicMock(return_value=mock_cursor)
        adapter._db.__getitem__ = MagicMock(return_value=mock_coll)

        result = await adapter.sample("users", limit=2)

        assert isinstance(result, FetchResult)
        assert len(result.rows) == 2

    @pytest.mark.asyncio
    async def test_disconnect(self, adapter):
        adapter._client = MagicMock()
        adapter._client.close = MagicMock()

        await adapter.disconnect()

        assert adapter._client is None
        assert adapter._db is None
        assert adapter.connected is False

    @pytest.mark.asyncio
    async def test_watcher_list_items(self, adapter):
        adapter._db.list_collection_names = AsyncMock(
            return_value=["users", "orders"],
        )

        items = await adapter.list_items()

        assert len(items) == 2
        assert all(isinstance(i, WatchItem) for i in items)
        paths = {i.path for i in items}
        assert paths == {"testdb.users", "testdb.orders"}

    @pytest.mark.asyncio
    async def test_watcher_list_items_with_pattern(self, adapter):
        adapter._db.list_collection_names = AsyncMock(
            return_value=["users", "orders", "logs"],
        )

        items = await adapter.list_items(patterns=["*users*"])

        assert len(items) == 1
        assert items[0].id == "users"


# ═══════════════════════════════════════════════════════════════════════
# RedisAdapter — Mocked Tests
# ═══════════════════════════════════════════════════════════════════════


class TestRedisAdapterMocked:
    """Test RedisAdapter with mocked redis client."""

    @pytest.fixture
    def adapter(self):
        from digitorn.modules.database.adapters.redis import RedisAdapter
        a = RedisAdapter()
        a._client = AsyncMock()
        a._db_index = 0
        return a

    def test_properties(self, adapter):
        assert adapter.connected is True
        assert adapter.driver_name == "redis"

    def test_not_connected(self):
        from digitorn.modules.database.adapters.redis import RedisAdapter
        a = RedisAdapter()
        assert a.connected is False

    @pytest.mark.asyncio
    async def test_not_connected_raises(self):
        from digitorn.modules.database.adapters.redis import RedisAdapter
        a = RedisAdapter()
        with pytest.raises(RuntimeError, match="Not connected"):
            await a.execute("GET key")

    @pytest.mark.asyncio
    async def test_execute_set(self, adapter):
        adapter._client.execute_command = AsyncMock(return_value=True)

        result = await adapter.execute("SET user:123 hello")

        assert isinstance(result, ExecuteResult)
        assert result.rows_affected == 1
        adapter._client.execute_command.assert_called_once_with(
            "SET", "user:123", "hello",
        )

    @pytest.mark.asyncio
    async def test_execute_del(self, adapter):
        adapter._client.execute_command = AsyncMock(return_value=2)

        result = await adapter.execute("DEL key1 key2")

        assert result.rows_affected == 2

    @pytest.mark.asyncio
    async def test_execute_hset(self, adapter):
        adapter._client.execute_command = AsyncMock(return_value=3)

        result = await adapter.execute("HSET user:1 name Alice age 30 email a@b.com")

        assert result.rows_affected == 3

    @pytest.mark.asyncio
    async def test_execute_lpush(self, adapter):
        adapter._client.execute_command = AsyncMock(return_value=5)

        result = await adapter.execute("LPUSH queue task1")

        assert result.rows_affected == 5

    @pytest.mark.asyncio
    async def test_execute_with_params(self, adapter):
        adapter._client.execute_command = AsyncMock(return_value=True)

        result = await adapter.execute("SET :p0 :p1", ["mykey", "myvalue"])

        adapter._client.execute_command.assert_called_once_with(
            "SET", "mykey", "myvalue",
        )

    @pytest.mark.asyncio
    async def test_fetch_get(self, adapter):
        adapter._client.execute_command = AsyncMock(return_value="hello")

        result = await adapter.fetch("GET user:123")

        assert isinstance(result, FetchResult)
        assert len(result.rows) == 1
        assert result.rows[0]["value"] == "hello"
        assert result.rows[0]["key"] == "user:123"

    @pytest.mark.asyncio
    async def test_fetch_hgetall(self, adapter):
        adapter._client.execute_command = AsyncMock(
            return_value={"name": "Alice", "age": "30"},
        )

        result = await adapter.fetch("HGETALL user:123")

        assert len(result.rows) == 2
        assert result.columns == ["key", "field", "value"]
        fields = {r["field"] for r in result.rows}
        assert fields == {"name", "age"}

    @pytest.mark.asyncio
    async def test_fetch_keys(self, adapter):
        adapter._client.execute_command = AsyncMock(
            return_value=["user:1", "user:2", "user:3"],
        )

        result = await adapter.fetch("KEYS user:*")

        assert len(result.rows) == 3
        assert result.columns == ["value"]

    @pytest.mark.asyncio
    async def test_fetch_none(self, adapter):
        adapter._client.execute_command = AsyncMock(return_value=None)

        result = await adapter.fetch("GET nonexistent")

        assert len(result.rows) == 0

    @pytest.mark.asyncio
    async def test_fetch_boolean(self, adapter):
        adapter._client.execute_command = AsyncMock(return_value=True)

        result = await adapter.fetch("EXISTS key")

        assert result.rows[0]["result"] is True

    @pytest.mark.asyncio
    async def test_table_stats(self, adapter):
        # Mock SCAN to return keys
        adapter._client.scan = AsyncMock(
            return_value=(0, ["user:1", "user:2", "user:3"]),
        )
        adapter._client.exists = AsyncMock(return_value=False)

        stats = await adapter.table_stats("user")

        assert isinstance(stats, TableStats)
        assert stats.name == "user"
        assert stats.row_count == 3

    @pytest.mark.asyncio
    async def test_sample(self, adapter):
        adapter._client.scan = AsyncMock(
            return_value=(0, ["user:1", "user:2"]),
        )
        adapter._client.exists = AsyncMock(return_value=False)
        adapter._client.type = AsyncMock(return_value="string")
        adapter._client.get = AsyncMock(return_value='{"name": "Alice"}')
        adapter._client.ttl = AsyncMock(return_value=-1)

        result = await adapter.sample("user", limit=2)

        assert isinstance(result, FetchResult)
        assert len(result.rows) == 2
        assert result.rows[0]["key"] == "user:1"
        assert result.rows[0]["type"] == "string"

    @pytest.mark.asyncio
    async def test_list_tables(self, adapter):
        # Simulate SCAN returning keys with prefixes
        adapter._client.scan = AsyncMock(
            return_value=(0, ["user:1", "user:2", "order:1", "config"]),
        )
        adapter._client.type = AsyncMock(return_value="string")

        tables = await adapter.list_tables()

        assert len(tables) == 3  # user, order, config
        names = {t.name for t in tables}
        assert names == {"user", "order", "config"}
        assert all(isinstance(t, TableInfo) for t in tables)

    @pytest.mark.asyncio
    async def test_introspect(self, adapter):
        adapter._client.scan = AsyncMock(
            return_value=(0, ["user:1"]),
        )
        adapter._client.type = AsyncMock(return_value="hash")

        schema = await adapter.introspect()

        assert isinstance(schema, SchemaInfo)
        assert schema.database == "db0"
        assert schema.driver == "redis"

    @pytest.mark.asyncio
    async def test_get_columns_hash(self, adapter):
        adapter._client.scan = AsyncMock(
            return_value=(0, ["user:1", "user:2"]),
        )
        adapter._client.type = AsyncMock(return_value="hash")
        adapter._client.hgetall = AsyncMock(
            return_value={"name": "Alice", "age": "30", "email": "a@b.com"},
        )

        columns = await adapter.get_columns("user")

        # Should have key + name + age + email
        assert len(columns) >= 4
        names = [c.name for c in columns]
        assert "key" in names
        assert "name" in names
        assert "age" in names

    @pytest.mark.asyncio
    async def test_get_columns_string(self, adapter):
        adapter._client.scan = AsyncMock(
            return_value=(0, ["config:app"]),
        )
        adapter._client.type = AsyncMock(return_value="string")

        columns = await adapter.get_columns("config")

        names = [c.name for c in columns]
        assert "key" in names
        assert "value" in names
        assert "type" in names

    @pytest.mark.asyncio
    async def test_disconnect(self, adapter):
        adapter._client.aclose = AsyncMock()

        await adapter.disconnect()

        assert adapter._client is None
        assert adapter.connected is False

    @pytest.mark.asyncio
    async def test_transaction_flow(self, adapter):
        mock_pipeline = MagicMock()
        mock_pipeline.execute = AsyncMock(return_value=[True])
        mock_pipeline.reset = AsyncMock()
        adapter._client.pipeline = MagicMock(return_value=mock_pipeline)

        # Begin
        await adapter.begin()
        assert adapter._in_transaction is True

        # Execute within transaction — queued to pipeline
        await adapter.execute("SET key value")

        # Commit
        await adapter.commit()
        assert adapter._in_transaction is False
        mock_pipeline.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_transaction_rollback(self, adapter):
        mock_pipeline = MagicMock()
        mock_pipeline.reset = AsyncMock()
        adapter._client.pipeline = MagicMock(return_value=mock_pipeline)

        await adapter.begin()
        await adapter.rollback()

        assert adapter._in_transaction is False
        mock_pipeline.reset.assert_called_once()

    @pytest.mark.asyncio
    async def test_watcher_list_items(self, adapter):
        adapter._client.scan = AsyncMock(
            return_value=(0, ["user:1", "user:2", "order:1"]),
        )

        items = await adapter.list_items()

        assert len(items) == 2  # user, order
        assert all(isinstance(i, WatchItem) for i in items)

    @pytest.mark.asyncio
    async def test_watcher_list_items_with_pattern(self, adapter):
        adapter._client.scan = AsyncMock(
            return_value=(0, ["user:1", "order:1"]),
        )

        items = await adapter.list_items(patterns=["*user*"])

        assert len(items) == 1
        assert items[0].id == "user"

    @pytest.mark.asyncio
    async def test_empty_command_raises(self, adapter):
        with pytest.raises(ValueError, match="Empty command"):
            await adapter.execute("")


# ═══════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════


class TestMongoHelpers:

    def test_substitute_params(self):
        from digitorn.modules.database.adapters.mongo import _substitute_params

        obj = {"name": ":p0", "age": {"$gt": ":p1"}}
        result = _substitute_params(obj, ["Alice", 25])
        assert result == {"name": "Alice", "age": {"$gt": 25}}

    def test_substitute_params_no_params(self):
        from digitorn.modules.database.adapters.mongo import _substitute_params

        obj = {"name": "Alice"}
        result = _substitute_params(obj, None)
        assert result == {"name": "Alice"}

    def test_substitute_params_list(self):
        from digitorn.modules.database.adapters.mongo import _substitute_params

        obj = [":p0", ":p1"]
        result = _substitute_params(obj, ["a", "b"])
        assert result == ["a", "b"]

    def test_serialize_doc(self):
        from digitorn.modules.database.adapters.mongo import _serialize_doc

        doc = {"name": "Alice", "nested": {"key": "value"}, "count": 42}
        result = _serialize_doc(doc)
        assert result == {"name": "Alice", "nested": {"key": "value"}, "count": 42}

    def test_serialize_doc_bytes(self):
        from digitorn.modules.database.adapters.mongo import _serialize_doc

        doc = {"data": b"\x00\x01\x02"}
        result = _serialize_doc(doc)
        assert result["data"] == "<binary 3 bytes>"

    def test_mongo_type(self):
        from digitorn.modules.database.adapters.mongo import _mongo_type

        assert _mongo_type("hello") == "string"
        assert _mongo_type(42) == "int"
        assert _mongo_type(3.14) == "double"
        assert _mongo_type(True) == "bool"
        assert _mongo_type([1, 2]) == "array"
        assert _mongo_type({"a": 1}) == "object"
        assert _mongo_type(None) == "null"
        assert _mongo_type(b"\x00") == "binary"

    def test_sanitize_mongo_url(self):
        from digitorn.modules.database.adapters.mongo import _sanitize_mongo_url

        url = "mongodb://admin:secret@host:27017/db"
        assert _sanitize_mongo_url(url) == "mongodb://admin:****@host:27017/db"

        url2 = "mongodb://host:27017/db"
        assert _sanitize_mongo_url(url2) == url2


class TestRedisHelpers:

    def test_substitute_args(self):
        from digitorn.modules.database.adapters.redis import _substitute_args

        args = [":p0", ":p1", "literal"]
        result = _substitute_args(args, ["key", "value"])
        assert result == ["key", "value", "literal"]

    def test_normalize_result_string(self):
        from digitorn.modules.database.adapters.redis import _normalize_result

        result = _normalize_result("GET", ["mykey"], "hello", 1000)
        assert result.rows[0]["key"] == "mykey"
        assert result.rows[0]["value"] == "hello"

    def test_normalize_result_dict(self):
        from digitorn.modules.database.adapters.redis import _normalize_result

        result = _normalize_result(
            "HGETALL", ["user:1"], {"name": "Alice", "age": "30"}, 1000,
        )
        assert len(result.rows) == 2
        assert result.columns == ["key", "field", "value"]

    def test_normalize_result_list(self):
        from digitorn.modules.database.adapters.redis import _normalize_result

        result = _normalize_result("KEYS", [], ["k1", "k2"], 1000)
        assert len(result.rows) == 2
        assert result.columns == ["value"]

    def test_normalize_result_none(self):
        from digitorn.modules.database.adapters.redis import _normalize_result

        result = _normalize_result("GET", ["k"], None, 1000)
        assert len(result.rows) == 0

    def test_normalize_result_scored(self):
        from digitorn.modules.database.adapters.redis import _normalize_result

        result = _normalize_result(
            "ZRANGE", ["lb"], [("alice", 100), ("bob", 95)], 1000,
        )
        assert result.columns == ["member", "score"]
        assert len(result.rows) == 2

    def test_redis_value_type(self):
        from digitorn.modules.database.adapters.redis import _redis_value_type

        assert _redis_value_type("string") == "string"
        assert _redis_value_type("hash") == "object"
        assert _redis_value_type("list") == "array"
        assert _redis_value_type("set") == "array"

    def test_sanitize_redis_url(self):
        from digitorn.modules.database.adapters.redis import _sanitize_redis_url

        url = "redis://:secret@localhost:6379/0"
        assert _sanitize_redis_url(url) == "redis://****@localhost:6379/0"

        url2 = "redis://localhost:6379/0"
        assert _sanitize_redis_url(url2) == url2


# ═══════════════════════════════════════════════════════════════════════
# ConnectionPool — NoSQL driver registration
# ═══════════════════════════════════════════════════════════════════════


class TestConnectionPoolNoSQL:

    def test_mongodb_in_factories(self):
        from digitorn.modules.database.connections import _ADAPTER_FACTORIES
        assert "mongodb" in _ADAPTER_FACTORIES

    def test_redis_in_factories(self):
        from digitorn.modules.database.connections import _ADAPTER_FACTORIES
        assert "redis" in _ADAPTER_FACTORIES

    def test_mongodb_in_url_schemes(self):
        from digitorn.modules.database.connections import _URL_SCHEMES
        assert _URL_SCHEMES["mongodb"] == "mongodb"

    def test_redis_in_url_schemes(self):
        from digitorn.modules.database.connections import _URL_SCHEMES
        assert _URL_SCHEMES["redis"] == "redis"


# ═══════════════════════════════════════════════════════════════════════
# DatabaseModule — NoSQL driver validation
# ═══════════════════════════════════════════════════════════════════════


class TestDatabaseModuleNoSQL:

    def test_driver_pattern_accepts_mongodb(self):
        from digitorn.modules.database.params import ConnectParams
        params = ConnectParams(
            connection_id="mongo", driver="mongodb", database="test",
        )
        assert params.driver == "mongodb"

    def test_driver_pattern_accepts_redis(self):
        from digitorn.modules.database.params import ConnectParams
        params = ConnectParams(
            connection_id="cache", driver="redis",
        )
        assert params.driver == "redis"

    def test_driver_pattern_rejects_invalid(self):
        from digitorn.modules.database.params import ConnectParams
        with pytest.raises(Exception):
            ConnectParams(
                connection_id="bad", driver="cassandra",
            )


# ═══════════════════════════════════════════════════════════════════════
# Async Iterator Mock — helper for MongoDB cursor simulation
# ═══════════════════════════════════════════════════════════════════════


class AsyncIteratorMock:
    """Mock for async iteration over MongoDB cursors."""

    def __init__(self, items: list):
        self._items = items
        self._index = 0

    def __aiter__(self):
        self._index = 0
        return self

    async def __anext__(self):
        if self._index >= len(self._items):
            raise StopAsyncIteration
        item = self._items[self._index]
        self._index += 1
        return item
