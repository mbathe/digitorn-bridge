"""MongoDB async adapter - maps collections/documents to the DatabaseAdapter protocol.

Uses ``motor`` (async MongoDB driver). Collections map to "tables", document
fields (inferred by sampling) map to "columns".

Query format for execute/fetch:
    JSON string parsed into MongoDB operations::

        {"collection": "users", "filter": {"age": {"$gt": 25}}}
        {"collection": "users", "filter": {}, "projection": {"name": 1}, "sort": {"age": -1}}

        {"collection": "users", "operation": "insert_one", "document": {"name": "Alice"}}
        {"collection": "users", "operation": "insert_many", "documents": [{...}, {...}]}

        {"collection": "users", "operation": "update_one",
         "filter": {"name": "Alice"}, "update": {"$set": {"age": 31}}}
        {"collection": "users", "operation": "update_many",
         "filter": {"active": false}, "update": {"$set": {"archived": true}}}

        {"collection": "users", "operation": "delete_one", "filter": {"name": "Alice"}}
        {"collection": "users", "operation": "delete_many", "filter": {"archived": true}}

        {"operation": "create_collection", "collection": "new_coll"}

        {"operation": "drop_collection", "collection": "old_coll"}
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
from collections import Counter
from typing import Any

import structlog

from .base import (
    ColumnInfo,
    ExecuteResult,
    FetchResult,
    ForeignKey,
    IndexInfo,
    ItemChecksum,
    SchemaInfo,
    TableInfo,
    TableStats,
    WatchItem,
)

logger = structlog.get_logger(__name__)

_SCHEMA_SAMPLE_SIZE = 100


class MongoAdapter:
    """Async MongoDB adapter backed by motor.

    Supports MongoDB 4.0+ (transactions require replica set).
    """

    def __init__(self) -> None:
        self._client: Any = None
        self._db: Any = None
        self._session: Any = None
        self._db_name: str = ""


    @property
    def connected(self) -> bool:
        return self._client is not None

    @property
    def driver_name(self) -> str:
        return "mongodb"


    async def connect(self, url: str, **options: Any) -> None:
        try:
            from motor.motor_asyncio import AsyncIOMotorClient
        except ImportError as exc:
            raise ImportError(
                "motor is required for MongoDB support. "
                "Install it with: pip install motor"
            ) from exc

        if self._client:
            await self.disconnect()

        self._client = AsyncIOMotorClient(url, **options)

        db_name = self._client.get_default_database()
        if db_name is not None:
            self._db = db_name
            self._db_name = db_name.name
        else:
            self._db_name = options.get("database", "test")
            self._db = self._client[self._db_name]

        await self._client.admin.command("ping")

        await logger.ainfo(
            "mongo_adapter_connected",
            database=self._db_name,
            url=_sanitize_mongo_url(url),
        )

    async def disconnect(self) -> None:
        if self._session:
            await self._session.end_session()
            self._session = None

        if self._client:
            self._client.close()
            self._client = None
            self._db = None


    async def execute(
        self, query: str, params: list[Any] | None = None,
    ) -> ExecuteResult:
        self._assert_connected()
        cmd = json.loads(query)

        operation = cmd.get("operation", "")
        collection_name = cmd.get("collection", "")
        session = self._session

        if operation == "create_collection":
            await self._db.create_collection(collection_name)
            return ExecuteResult(rows_affected=0)

        if operation == "drop_collection":
            await self._db[collection_name].drop(session=session)
            return ExecuteResult(rows_affected=0)

        coll = self._db[collection_name]

        if operation == "insert_one":
            doc = _substitute_params(cmd["document"], params)
            result = await coll.insert_one(doc, session=session)
            return ExecuteResult(
                rows_affected=1,
                last_insert_id=str(result.inserted_id),
            )

        if operation == "insert_many":
            docs = [_substitute_params(d, params) for d in cmd["documents"]]
            result = await coll.insert_many(docs, session=session)
            return ExecuteResult(rows_affected=len(result.inserted_ids))

        if operation in ("update_one", "update_many"):
            filter_ = _substitute_params(cmd.get("filter", {}), params)
            update = _substitute_params(cmd.get("update", {}), params)
            fn = coll.update_one if operation == "update_one" else coll.update_many
            result = await fn(filter_, update, session=session)
            return ExecuteResult(rows_affected=result.modified_count)

        if operation in ("delete_one", "delete_many"):
            filter_ = _substitute_params(cmd.get("filter", {}), params)
            fn = coll.delete_one if operation == "delete_one" else coll.delete_many
            result = await fn(filter_, session=session)
            return ExecuteResult(rows_affected=result.deleted_count)

        raise ValueError(
            f"Unknown operation: {operation!r}. "
            "Supported: insert_one, insert_many, update_one, update_many, "
            "delete_one, delete_many, create_collection, drop_collection."
        )

    async def fetch(
        self,
        query: str,
        params: list[Any] | None = None,
        limit: int = 1000,
    ) -> FetchResult:
        self._assert_connected()
        cmd = json.loads(query)

        collection_name = cmd["collection"]
        filter_ = _substitute_params(cmd.get("filter", {}), params)
        projection = cmd.get("projection")
        sort = cmd.get("sort")

        coll = self._db[collection_name]
        cursor = coll.find(filter_, projection, session=self._session)

        if sort:
            sort_list = [(k, v) for k, v in sort.items()]
            cursor = cursor.sort(sort_list)

        cursor = cursor.limit(limit)

        rows: list[dict[str, Any]] = []
        columns_set: set[str] = set()
        async for doc in cursor:
            row = _serialize_doc(doc)
            columns_set.update(row.keys())
            rows.append(row)

        columns = sorted(columns_set)
        return FetchResult(columns=columns, rows=rows, total_count=len(rows))


    async def introspect(self, schema: str | None = None) -> SchemaInfo:
        self._assert_connected()
        tables = await self.list_tables(schema)
        return SchemaInfo(
            tables=tables,
            database=self._db_name,
            driver="mongodb",
        )

    async def list_tables(self, schema: str | None = None) -> list[TableInfo]:
        self._assert_connected()
        collection_names = await self._db.list_collection_names()
        collection_names = [
            n for n in collection_names
            if not n.startswith("system.")
        ]

        tables: list[TableInfo] = []
        for name in sorted(collection_names):
            columns = await self._infer_columns(name)
            indexes = await self._get_indexes(name)
            tables.append(TableInfo(
                name=name,
                schema=schema,
                columns=columns,
                foreign_keys=[],
                indexes=indexes,
                comment=None,
            ))

        return tables

    async def get_columns(self, table: str) -> list[ColumnInfo]:
        self._assert_connected()
        return await self._infer_columns(table)

    async def table_stats(self, table: str) -> TableStats:
        self._assert_connected()
        coll = self._db[table]
        count = await coll.estimated_document_count()
        try:
            stats = await self._db.command("collStats", table)
            size_bytes = stats.get("size", None)
        except Exception:
            size_bytes = None
        return TableStats(name=table, row_count=count, size_bytes=size_bytes)

    async def sample(self, table: str, limit: int = 5) -> FetchResult:
        self._assert_connected()
        coll = self._db[table]
        cursor = coll.find().limit(limit)

        rows: list[dict[str, Any]] = []
        columns_set: set[str] = set()
        async for doc in cursor:
            row = _serialize_doc(doc)
            columns_set.update(row.keys())
            rows.append(row)

        columns = sorted(columns_set)
        return FetchResult(columns=columns, rows=rows, total_count=len(rows))


    async def begin(self) -> None:
        self._assert_connected()
        if self._session:
            return
        self._session = await self._client.start_session()
        self._session.start_transaction()

    async def commit(self) -> None:
        if not self._session:
            return
        await self._session.commit_transaction()
        await self._session.end_session()
        self._session = None

    async def rollback(self) -> None:
        if not self._session:
            return
        await self._session.abort_transaction()
        await self._session.end_session()
        self._session = None


    async def explain(
        self, query: str, params: list[Any] | None = None, analyze: bool = False,
    ) -> list[dict[str, Any]]:
        self._assert_connected()
        cmd = json.loads(query)
        collection_name = cmd["collection"]
        filter_ = _substitute_params(cmd.get("filter", {}), params)

        coll = self._db[collection_name]
        cursor = coll.find(filter_)
        plan = await cursor.explain()

        return [plan] if isinstance(plan, dict) else plan

    async def ping(self) -> bool:
        if not self._client:
            return False
        try:
            await self._client.admin.command("ping")
            return True
        except Exception:
            return False


    async def list_items(
        self, patterns: list[str] | None = None,
    ) -> list[WatchItem]:
        self._assert_connected()
        collection_names = await self._db.list_collection_names()
        collection_names = [n for n in collection_names if not n.startswith("system.")]

        items = []
        for name in collection_names:
            path = f"{self._db_name}.{name}"
            if patterns:
                if not any(
                    fnmatch.fnmatch(path, p) or fnmatch.fnmatch(name, p)
                    for p in patterns
                ):
                    continue
            items.append(WatchItem(id=name, path=path))

        return items

    async def checksum(self, items: list[str]) -> list[ItemChecksum]:
        self._assert_connected()
        checksums = []
        for item_id in items:
            try:
                coll = self._db[item_id]
                count = await coll.estimated_document_count()
                sample_docs = []
                async for doc in coll.find().limit(10):
                    sample_docs.append(sorted(doc.keys()))
                schema_repr = str(sample_docs)
                hash_input = f"{schema_repr}:{count}"
                h = hashlib.sha256(hash_input.encode()).hexdigest()[:16]
                checksums.append(ItemChecksum(id=item_id, hash=h))
            except Exception:
                checksums.append(ItemChecksum(id=item_id, hash=""))
        return checksums


    def _assert_connected(self) -> None:
        if not self._client:
            raise RuntimeError("Not connected. Call connect() first.")

    async def _infer_columns(self, collection_name: str) -> list[ColumnInfo]:
        """Infer document schema by sampling documents."""
        coll = self._db[collection_name]
        field_types: dict[str, Counter] = {}
        field_count: Counter = Counter()

        sample_size = _SCHEMA_SAMPLE_SIZE
        total = await coll.estimated_document_count()

        async for doc in coll.find().limit(sample_size):
            for key, value in doc.items():
                field_count[key] += 1
                if key not in field_types:
                    field_types[key] = Counter()
                field_types[key][_mongo_type(value)] += 1

        if not field_types:
            return []

        sampled = min(sample_size, total) if total > 0 else 0
        columns = []
        for field_name in sorted(field_types.keys()):
            most_common_type = field_types[field_name].most_common(1)[0][0]
            nullable = field_count[field_name] < sampled if sampled > 0 else True
            columns.append(ColumnInfo(
                name=field_name,
                type=most_common_type,
                nullable=nullable,
                primary_key=(field_name == "_id"),
                default=None,
                comment=None,
            ))

        return columns

    async def _get_indexes(self, collection_name: str) -> list[IndexInfo]:
        """Get indexes for a collection."""
        coll = self._db[collection_name]
        indexes = []
        async for idx in coll.list_indexes():
            name = idx.get("name", "")
            if name == "_id_":
                continue
            key_fields = list(idx.get("key", {}).keys())
            unique = idx.get("unique", False)
            indexes.append(IndexInfo(
                name=name,
                columns=key_fields,
                unique=unique,
            ))
        return indexes


def _mongo_type(value: Any) -> str:
    """Map Python/BSON types to human-readable type strings."""
    if value is None:
        return "null"
    type_map = {
        bool: "bool",
        str: "string",
        int: "int",
        float: "double",
        list: "array",
        dict: "object",
        bytes: "binary",
    }
    for py_type, name in type_map.items():
        if isinstance(value, py_type):
            return name

    type_name = type(value).__name__
    bson_map = {
        "ObjectId": "objectId",
        "datetime": "date",
        "Decimal128": "decimal",
        "Regex": "regex",
        "Binary": "binary",
    }
    return bson_map.get(type_name, type_name)


def _serialize_doc(doc: dict[str, Any]) -> dict[str, Any]:
    """Serialize a MongoDB document to JSON-safe dict."""
    result = {}
    for key, value in doc.items():
        if hasattr(value, "__str__") and type(value).__name__ == "ObjectId":
            result[key] = str(value)
        elif isinstance(value, bytes):
            result[key] = f"<binary {len(value)} bytes>"
        elif isinstance(value, dict):
            result[key] = _serialize_doc(value)
        elif isinstance(value, list):
            result[key] = [
                _serialize_doc(v) if isinstance(v, dict) else
                str(v) if hasattr(v, "__str__") and type(v).__name__ == "ObjectId"
                else v
                for v in value
            ]
        else:
            result[key] = value
    return result


def _substitute_params(obj: Any, params: list[Any] | None) -> Any:
    """Replace :p0, :p1, ... placeholders in a MongoDB query object."""
    if not params:
        return obj
    if isinstance(obj, str):
        for i, val in enumerate(params):
            placeholder = f":p{i}"
            if obj == placeholder:
                return val
            if placeholder in obj:
                obj = obj.replace(placeholder, str(val))
        return obj
    if isinstance(obj, dict):
        return {k: _substitute_params(v, params) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_substitute_params(item, params) for item in obj]
    return obj


def _sanitize_mongo_url(url: str) -> str:
    """Remove password from MongoDB URL for logging."""
    if "@" in url and ":" in url.split("@")[0]:
        parts = url.split("@")
        creds = parts[0]
        scheme_user = creds.rsplit(":", 1)[0]
        return f"{scheme_user}:****@{'@'.join(parts[1:])}"
    return url
