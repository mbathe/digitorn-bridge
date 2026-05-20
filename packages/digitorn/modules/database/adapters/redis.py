"""Redis async adapter - maps key spaces to the DatabaseAdapter protocol."""

from __future__ import annotations

import fnmatch
import hashlib
import shlex
from collections import Counter
from typing import Any

import structlog

from .base import (
    ColumnInfo,
    ExecuteResult,
    FetchResult,
    IndexInfo,
    ItemChecksum,
    SchemaInfo,
    TableInfo,
    TableStats,
    WatchItem,
)

logger = structlog.get_logger(__name__)

_SCAN_LIMIT = 10_000

_READ_COMMANDS = frozenset({
    "GET", "MGET", "HGET", "HGETALL", "HMGET", "HKEYS", "HVALS", "HLEN",
    "LRANGE", "LLEN", "LINDEX",
    "SMEMBERS", "SCARD", "SISMEMBER",
    "ZRANGE", "ZRANGEBYSCORE", "ZCARD", "ZSCORE", "ZRANK",
    "KEYS", "SCAN", "TYPE", "TTL", "PTTL", "EXISTS",
    "DBSIZE", "INFO", "CONFIG",
})

class RedisAdapter:
    """Async Redis adapter."""

    def __init__(self) -> None:
        self._client: Any = None
        self._db_index: int = 0
        self._in_transaction: bool = False
        self._pipeline: Any = None

    @property
    def connected(self) -> bool:
        return self._client is not None

    @property
    def driver_name(self) -> str:
        return "redis"

    async def connect(self, url: str, **options: Any) -> None:
        try:
            import redis.asyncio as aioredis
        except ImportError as exc:
            raise ImportError(
                "redis is required for Redis support. "
                "Install it with: pip install redis[hiredis]"
            ) from exc

        if self._client:
            await self.disconnect()

        self._client = aioredis.from_url(url, decode_responses=True, **options)

        await self._client.ping()

        try:
            path = url.rstrip("/").rsplit("/", 1)
            if len(path) == 2 and path[1].isdigit():
                self._db_index = int(path[1])
        except Exception:
            pass  # Non-critical: db index parsing is best-effort, defaults to 0

        await logger.ainfo(
            "redis_adapter_connected",
            db_index=self._db_index,
            url=_sanitize_redis_url(url),
        )

    async def disconnect(self) -> None:
        if self._pipeline:
            self._pipeline = None
            self._in_transaction = False

        if self._client:
            await self._client.aclose()
            self._client = None

    async def execute(
        self, query: str, params: list[Any] | None = None,
    ) -> ExecuteResult:
        self._assert_connected()
        parts = shlex.split(query)
        if not parts:
            raise ValueError("Empty command.")

        cmd = parts[0].upper()
        args = parts[1:]

        if params:
            args = _substitute_args(args, params)

        if self._in_transaction and self._pipeline:
            getattr(self._pipeline, cmd.lower())(*args)
            return ExecuteResult(rows_affected=0)

        result = await self._client.execute_command(cmd, *args)

        rows_affected = 0
        if cmd in ("SET", "SETEX", "PSETEX", "SETNX", "RENAME"):
            rows_affected = 1
        elif cmd in ("DEL", "UNLINK"):
            rows_affected = int(result) if result else 0
        elif cmd in ("HSET", "HMSET"):
            rows_affected = int(result) if isinstance(result, int) else 1
        elif cmd in ("LPUSH", "RPUSH", "SADD", "ZADD"):
            rows_affected = int(result) if isinstance(result, int) else 0
        elif cmd == "EXPIRE":
            rows_affected = int(result) if result else 0

        return ExecuteResult(rows_affected=rows_affected)

    async def fetch(
        self,
        query: str,
        params: list[Any] | None = None,
        limit: int = 1000,
    ) -> FetchResult:
        self._assert_connected()
        parts = shlex.split(query)
        if not parts:
            raise ValueError("Empty command.")

        cmd = parts[0].upper()
        args = parts[1:]

        if params:
            args = _substitute_args(args, params)

        result = await self._client.execute_command(cmd, *args)

        return _normalize_result(cmd, args, result, limit)

    async def introspect(self, schema: str | None = None) -> SchemaInfo:
        self._assert_connected()
        tables = await self.list_tables(schema)
        return SchemaInfo(
            tables=tables,
            database=f"db{self._db_index}",
            driver="redis",
        )

    async def list_tables(self, schema: str | None = None) -> list[TableInfo]:
        """Group keys by prefix (before first ':') as virtual tables."""
        self._assert_connected()

        prefixes: dict[str, Counter] = {}
        prefix_count: Counter = Counter()

        cursor = 0
        scanned = 0
        while scanned < _SCAN_LIMIT:
            cursor, keys = await self._client.scan(cursor, count=200)
            for key in keys:
                prefix = key.split(":")[0] if ":" in key else key
                prefix_count[prefix] += 1
                key_type = await self._client.type(key)
                if prefix not in prefixes:
                    prefixes[prefix] = Counter()
                prefixes[prefix][key_type] += 1
                scanned += 1
            if cursor == 0:
                break

        tables = []
        for prefix in sorted(prefixes.keys()):
            type_counter = prefixes[prefix]
            count = prefix_count[prefix]
            columns = [
                ColumnInfo(
                    name="key",
                    type="string",
                    nullable=False,
                    primary_key=True,
                    comment="Full Redis key",
                ),
                ColumnInfo(
                    name="type",
                    type=type_counter.most_common(1)[0][0],
                    nullable=False,
                    comment=f"Key types: {dict(type_counter)}",
                ),
                ColumnInfo(
                    name="value",
                    type=_redis_value_type(type_counter.most_common(1)[0][0]),
                    nullable=True,
                    comment="Key value (type depends on Redis data type)",
                ),
                ColumnInfo(
                    name="ttl",
                    type="int",
                    nullable=True,
                    comment="Time-to-live in seconds (-1 = no expiry, -2 = expired)",
                ),
            ]
            tables.append(TableInfo(
                name=prefix,
                schema=schema,
                columns=columns,
                foreign_keys=[],
                indexes=[],
                comment=f"{count} key(s), types: {dict(type_counter)}",
            ))

        return tables

    async def get_columns(self, table: str) -> list[ColumnInfo]:
        """Get column info for a key prefix."""
        self._assert_connected()

        keys = []
        cursor = 0
        while len(keys) < 20:
            cursor, batch = await self._client.scan(
                cursor, match=f"{table}:*", count=100,
            )
            keys.extend(batch)
            if cursor == 0:
                break

        if not keys:
            if await self._client.exists(table):
                keys = [table]

        if not keys:
            return []

        sample_key = keys[0]
        key_type = await self._client.type(sample_key)

        if key_type == "hash":
            return await self._infer_hash_columns(keys[:20])

        return [
            ColumnInfo(name="key", type="string", nullable=False, primary_key=True),
            ColumnInfo(name="type", type=key_type, nullable=False),
            ColumnInfo(name="value", type=_redis_value_type(key_type), nullable=True),
            ColumnInfo(name="ttl", type="int", nullable=True),
        ]

    async def table_stats(self, table: str) -> TableStats:
        """Count keys matching a prefix."""
        self._assert_connected()
        count = 0
        cursor = 0
        while True:
            cursor, keys = await self._client.scan(
                cursor, match=f"{table}:*", count=500,
            )
            count += len(keys)
            if cursor == 0:
                break

        if await self._client.exists(table):
            count += 1

        return TableStats(name=table, row_count=count)

    async def sample(self, table: str, limit: int = 5) -> FetchResult:
        """Sample keys matching a prefix and return their values."""
        self._assert_connected()

        keys: list[str] = []
        cursor = 0
        while len(keys) < limit:
            cursor, batch = await self._client.scan(
                cursor, match=f"{table}:*", count=100,
            )
            keys.extend(batch)
            if cursor == 0:
                break

        if len(keys) < limit and await self._client.exists(table):
            keys.append(table)

        keys = keys[:limit]

        rows: list[dict[str, Any]] = []
        for key in keys:
            key_type = await self._client.type(key)
            value = await self._get_value(key, key_type)
            ttl = await self._client.ttl(key)
            rows.append({
                "key": key,
                "type": key_type,
                "value": value,
                "ttl": ttl,
            })

        return FetchResult(
            columns=["key", "type", "value", "ttl"],
            rows=rows,
            total_count=len(rows),
        )

    async def begin(self) -> None:
        self._assert_connected()
        if self._in_transaction:
            return
        self._pipeline = self._client.pipeline(transaction=True)
        self._in_transaction = True

    async def commit(self) -> None:
        if not self._in_transaction or not self._pipeline:
            return
        await self._pipeline.execute()
        self._pipeline = None
        self._in_transaction = False

    async def rollback(self) -> None:
        if not self._in_transaction or not self._pipeline:
            return
        await self._pipeline.reset()
        self._pipeline = None
        self._in_transaction = False

    async def explain(
        self, query: str, params: list[Any] | None = None, analyze: bool = False,
    ) -> list[dict[str, Any]]:
        """Redis has no query plans. Return command analysis instead."""
        parts = shlex.split(query)
        cmd = parts[0].upper() if parts else ""
        return [{
            "command": cmd,
            "args": parts[1:],
            "note": "Redis does not support EXPLAIN. This is a command breakdown.",
            "complexity": _redis_command_complexity(cmd),
        }]

    async def ping(self) -> bool:
        if not self._client:
            return False
        try:
            return await self._client.ping()
        except Exception:
            return False

    async def list_items(
        self, patterns: list[str] | None = None,
    ) -> list[WatchItem]:
        self._assert_connected()

        prefixes: set[str] = set()
        cursor = 0
        scanned = 0
        while scanned < _SCAN_LIMIT:
            cursor, keys = await self._client.scan(cursor, count=200)
            for key in keys:
                prefix = key.split(":")[0] if ":" in key else key
                prefixes.add(prefix)
                scanned += 1
            if cursor == 0:
                break

        items = []
        for prefix in sorted(prefixes):
            path = f"db{self._db_index}.{prefix}"
            if patterns:
                if not any(
                    fnmatch.fnmatch(path, p) or fnmatch.fnmatch(prefix, p)
                    for p in patterns
                ):
                    continue
            items.append(WatchItem(id=prefix, path=path))

        return items

    async def checksum(self, items: list[str]) -> list[ItemChecksum]:
        self._assert_connected()
        checksums = []
        for item_id in items:
            try:
                count = 0
                sample_types: list[str] = []
                cursor = 0
                while True:
                    cursor, keys = await self._client.scan(
                        cursor, match=f"{item_id}:*", count=200,
                    )
                    count += len(keys)
                    for key in keys[:5]:
                        t = await self._client.type(key)
                        sample_types.append(t)
                    if cursor == 0:
                        break

                hash_input = f"{sorted(sample_types)}:{count}"
                h = hashlib.sha256(hash_input.encode()).hexdigest()[:16]
                checksums.append(ItemChecksum(id=item_id, hash=h))
            except Exception:
                checksums.append(ItemChecksum(id=item_id, hash=""))
        return checksums

    def _assert_connected(self) -> None:
        if not self._client:
            raise RuntimeError("Not connected. Call connect() first.")

    async def _get_value(self, key: str, key_type: str) -> Any:
        try:
            if key_type == "string":
                return await self._client.get(key)
            elif key_type == "hash":
                return await self._client.hgetall(key)
            elif key_type == "list":
                return await self._client.lrange(key, 0, 99)
            elif key_type == "set":
                return list(await self._client.smembers(key))
            elif key_type == "zset":
                return await self._client.zrange(key, 0, 99, withscores=True)
            elif key_type == "stream":
                return await self._client.xrange(key, count=10)
            else:
                return f"<{key_type}>"
        except Exception:
            return None

    async def _infer_hash_columns(self, keys: list[str]) -> list[ColumnInfo]:
        field_count: Counter = Counter()
        total_sampled = 0

        for key in keys:
            fields = await self._client.hgetall(key)
            for field_name in fields:
                field_count[field_name] += 1
            total_sampled += 1

        columns = [
            ColumnInfo(
                name="key",
                type="string",
                nullable=False,
                primary_key=True,
                comment="Full Redis key",
            ),
        ]

        for field_name in sorted(field_count.keys()):
            nullable = field_count[field_name] < total_sampled
            columns.append(ColumnInfo(
                name=field_name,
                type="string",
                nullable=nullable,
            ))

        return columns

def _redis_value_type(key_type: str) -> str:
    return {
        "string": "string",
        "hash": "object",
        "list": "array",
        "set": "array",
        "zset": "array<score,member>",
        "stream": "array<id,fields>",
    }.get(key_type, key_type)

def _substitute_args(args: list[str], params: list[Any]) -> list[str]:
    result = []
    for arg in args:
        for i, val in enumerate(params):
            placeholder = f":p{i}"
            if arg == placeholder:
                arg = str(val)
                break
            if placeholder in arg:
                arg = arg.replace(placeholder, str(val))
        result.append(arg)
    return result

def _normalize_result(
    cmd: str, args: list[str], result: Any, limit: int,
) -> FetchResult:
    if result is True or result is False:
        return FetchResult(
            columns=["result"],
            rows=[{"result": result}],
            total_count=1,
        )

    if isinstance(result, (str, int, float)):
        key = args[0] if args else ""
        return FetchResult(
            columns=["key", "value"],
            rows=[{"key": key, "value": result}],
            total_count=1,
        )

    if isinstance(result, dict):
        key = args[0] if args else ""
        rows = [{"key": key, "field": k, "value": v} for k, v in result.items()]
        return FetchResult(
            columns=["key", "field", "value"],
            rows=rows[:limit],
            total_count=len(rows),
        )

    if isinstance(result, (list, set)):
        items = list(result)[:limit]
        if items and isinstance(items[0], (list, tuple)) and len(items[0]) == 2:
            rows = [{"member": m, "score": s} for m, s in items]
            return FetchResult(
                columns=["member", "score"],
                rows=rows,
                total_count=len(items),
            )
        rows = [{"value": item} for item in items]
        return FetchResult(
            columns=["value"],
            rows=rows,
            total_count=len(items),
        )

    if result is None:
        return FetchResult(columns=["value"], rows=[], total_count=0)

    return FetchResult(
        columns=["result"],
        rows=[{"result": str(result)}],
        total_count=1,
    )

def _redis_command_complexity(cmd: str) -> str:
    complexities = {
        "GET": "O(1)", "SET": "O(1)", "DEL": "O(1)", "EXISTS": "O(1)",
        "HGET": "O(1)", "HSET": "O(1)", "HGETALL": "O(N)",
        "LPUSH": "O(1)", "RPUSH": "O(1)", "LRANGE": "O(S+N)",
        "SADD": "O(1)", "SMEMBERS": "O(N)", "SCARD": "O(1)",
        "ZADD": "O(log N)", "ZRANGE": "O(log N + M)", "ZCARD": "O(1)",
        "KEYS": "O(N) - avoid in production", "SCAN": "O(1) per call",
        "MGET": "O(N)", "HMGET": "O(N)",
    }
    return complexities.get(cmd, "unknown")

def _sanitize_redis_url(url: str) -> str:
    if "@" in url:
        parts = url.split("@")
        scheme_part = parts[0]
        if ":" in scheme_part:
            prefix = scheme_part.split("://")[0]
            return f"{prefix}://****@{'@'.join(parts[1:])}"
    return url
