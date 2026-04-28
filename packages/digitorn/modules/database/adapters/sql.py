"""SQLAlchemy async adapter - covers PostgreSQL, MySQL, SQLite, MSSQL, Oracle.

This is the primary adapter. It uses SQLAlchemy's async engine for connection
pooling, and the inspector API for schema introspection. Queries go through
the native driver with zero abstraction overhead.

Performance notes:
    - Connection pooling: SQLAlchemy's QueuePool (default for non-SQLite).
    - Prepared statements: Used automatically by asyncpg/aiomysql.
    - Streaming: Large results are fetched with server-side cursors when
      supported by the driver.
"""

from __future__ import annotations

import fnmatch
import hashlib
from typing import Any

import structlog
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    create_async_engine,
)

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

_DRIVER_MAP = {
    "sqlite": "sqlite",
    "postgresql": "postgresql",
    "mysql": "mysql",
    "mssql": "mssql",
    "oracle": "oracle",
}


class SQLAdapter:
    """Async SQL adapter backed by SQLAlchemy.

    Supports any database that has an async SQLAlchemy driver:
        - SQLite:     ``sqlite+aiosqlite:///path.db``
        - PostgreSQL: ``postgresql+asyncpg://user:pass@host/db``
        - MySQL:      ``mysql+aiomysql://user:pass@host/db``
    """

    def __init__(self) -> None:
        self._engine: AsyncEngine | None = None
        self._conn: AsyncConnection | None = None
        self._in_transaction = False


    @property
    def connected(self) -> bool:
        return self._engine is not None

    @property
    def driver_name(self) -> str:
        if not self._engine:
            return ""
        dialect = self._engine.dialect.name
        return _DRIVER_MAP.get(dialect, dialect)


    async def connect(self, url: str, **options: Any) -> None:
        if self._engine:
            await self.disconnect()

        engine_kwargs: dict[str, Any] = {
            "echo": options.get("echo", False),
        }

        if not url.startswith("sqlite"):
            engine_kwargs["pool_size"] = options.get("pool_size", 5)
            engine_kwargs["max_overflow"] = options.get("max_overflow", 10)
            engine_kwargs["pool_recycle"] = options.get("pool_recycle", 3600)

        self._engine = create_async_engine(url, **engine_kwargs)

        async with self._engine.connect() as conn:
            await conn.execute(text("SELECT 1"))

        await logger.ainfo(
            "sql_adapter_connected",
            driver=self.driver_name,
            url=_sanitize_url(url),
        )

    async def disconnect(self) -> None:
        if self._conn:
            if self._in_transaction:
                await self._conn.rollback()
                self._in_transaction = False
            await self._conn.close()
            self._conn = None

        if self._engine:
            await self._engine.dispose()
            self._engine = None


    async def execute(
        self, query: str, params: list[Any] | None = None,
    ) -> ExecuteResult:
        self._assert_connected()
        bound_params = _bind_params(params)

        async with self._get_connection() as conn:
            result = await conn.execute(text(query), bound_params)
            if not self._in_transaction:
                await conn.commit()
            return ExecuteResult(
                rows_affected=result.rowcount,
                last_insert_id=getattr(result, "lastrowid", None),
            )

    async def execute_many(
        self,
        query: str,
        params_list: list[dict[str, Any]],
    ) -> ExecuteResult:
        """Execute a statement with multiple parameter sets (batch).

        Uses SQLAlchemy's ``execute()`` with a list of dicts which maps
        to the driver's ``executemany`` - far faster than looping in Python.
        """
        self._assert_connected()
        async with self._get_connection() as conn:
            result = await conn.execute(text(query), params_list)
            if not self._in_transaction:
                await conn.commit()
            return ExecuteResult(
                rows_affected=result.rowcount,
                last_insert_id=getattr(result, "lastrowid", None),
            )

    async def fetch(
        self,
        query: str,
        params: list[Any] | None = None,
        limit: int = 1000,
    ) -> FetchResult:
        self._assert_connected()
        bound_params = _bind_params(params)

        q_upper = query.strip().upper()
        if "LIMIT" not in q_upper and q_upper.startswith("SELECT"):
            query = f"{query.rstrip().rstrip(';')} LIMIT {int(limit)}"

        async with self._get_connection() as conn:
            result = await conn.execute(text(query), bound_params)
            columns = list(result.keys())
            rows: list[dict[str, Any]] = []
            while True:
                batch = result.fetchmany(1000)
                if not batch:
                    break
                rows.extend(dict(row._mapping) for row in batch)
                if len(rows) >= limit:
                    rows = rows[:limit]
                    break

        return FetchResult(columns=columns, rows=rows, total_count=len(rows))


    async def introspect(self, schema: str | None = None) -> SchemaInfo:
        self._assert_connected()
        tables = await self.list_tables(schema)
        return SchemaInfo(
            tables=tables,
            database=str(self._engine.url.database) if self._engine else "",
            driver=self.driver_name,
        )

    async def list_tables(self, schema: str | None = None) -> list[TableInfo]:
        self._assert_connected()

        async with self._engine.connect() as conn:  # type: ignore[union-attr]
            table_names = await conn.run_sync(
                lambda sync_conn: inspect(sync_conn).get_table_names(schema=schema)
            )

            tables: list[TableInfo] = []
            for tname in table_names:
                columns = await conn.run_sync(
                    lambda sync_conn, t=tname: [
                        ColumnInfo(
                            name=c["name"],
                            type=str(c["type"]),
                            nullable=c.get("nullable", True),
                            primary_key=c.get("primary_key", False)
                            if isinstance(c.get("primary_key"), bool)
                            else (c["name"] in _get_pk_columns(sync_conn, t, schema)),
                            default=str(c["default"]) if c.get("default") else None,
                            comment=c.get("comment"),
                        )
                        for c in inspect(sync_conn).get_columns(t, schema=schema)
                    ]
                )
                fks = await conn.run_sync(
                    lambda sync_conn, t=tname: [
                        ForeignKey(
                            column=fk["constrained_columns"][0] if fk["constrained_columns"] else "",
                            referred_table=fk["referred_table"],
                            referred_column=fk["referred_columns"][0] if fk["referred_columns"] else "",
                        )
                        for fk in inspect(sync_conn).get_foreign_keys(t, schema=schema)
                    ]
                )
                indexes = await conn.run_sync(
                    lambda sync_conn, t=tname: [
                        IndexInfo(
                            name=idx.get("name", ""),
                            columns=list(idx.get("column_names", [])),
                            unique=idx.get("unique", False),
                        )
                        for idx in inspect(sync_conn).get_indexes(t, schema=schema)
                        if idx.get("name")
                    ]
                )
                try:
                    comment = await conn.run_sync(
                        lambda sync_conn, t=tname: inspect(sync_conn).get_table_comment(
                            t, schema=schema
                        ).get("text"),
                    )
                except NotImplementedError:
                    comment = None

                tables.append(TableInfo(
                    name=tname,
                    schema=schema,
                    columns=columns,
                    foreign_keys=fks,
                    indexes=indexes,
                    comment=comment,
                ))

        return tables

    async def get_columns(self, table: str) -> list[ColumnInfo]:
        self._assert_connected()
        async with self._engine.connect() as conn:  # type: ignore[union-attr]
            return await conn.run_sync(
                lambda sync_conn: [
                    ColumnInfo(
                        name=c["name"],
                        type=str(c["type"]),
                        nullable=c.get("nullable", True),
                        primary_key=c["name"] in _get_pk_columns(sync_conn, table, None),
                        default=str(c["default"]) if c.get("default") else None,
                        comment=c.get("comment"),
                    )
                    for c in inspect(sync_conn).get_columns(table)
                ]
            )

    async def table_stats(self, table: str) -> TableStats:
        """Get row count, using fast approximate counts for large tables.

        PostgreSQL: uses ``pg_stat_user_tables`` (maintained by autovacuum).
        MySQL: uses ``information_schema.tables`` (approximate).
        SQLite/others: falls back to ``COUNT(*)``.
        """
        self._assert_connected()
        driver = self.driver_name

        async with self._engine.connect() as conn:  # type: ignore[union-attr]
            if driver == "postgresql":
                result = await conn.execute(text(
                    "SELECT n_live_tup FROM pg_stat_user_tables "
                    "WHERE relname = :table_name"
                ), {"table_name": table})
                row = result.fetchone()
                if row and row[0] > 0:
                    return TableStats(name=table, row_count=row[0])
            elif driver == "mysql":
                result = await conn.execute(text(
                    "SELECT table_rows FROM information_schema.tables "
                    "WHERE table_name = :table_name AND table_schema = DATABASE()"
                ), {"table_name": table})
                row = result.fetchone()
                if row and row[0] is not None and row[0] > 0:
                    return TableStats(name=table, row_count=row[0])

            result = await conn.execute(
                text(f"SELECT COUNT(*) AS cnt FROM {_quote_ident(table)}")
            )
            row = result.fetchone()
            count = row[0] if row else 0

        return TableStats(name=table, row_count=count)

    async def sample(self, table: str, limit: int = 5) -> FetchResult:
        return await self.fetch(
            f"SELECT * FROM {_quote_ident(table)} LIMIT {limit}"
        )


    async def explain(
        self, query: str, params: list[Any] | None = None, analyze: bool = False,
    ) -> list[dict[str, Any]]:
        """Return the query execution plan."""
        self._assert_connected()
        bound_params = _bind_params(params)

        prefix = "EXPLAIN ANALYZE" if analyze else "EXPLAIN"
        if self.driver_name == "sqlite" and not analyze:
            prefix = "EXPLAIN QUERY PLAN"

        explain_query = f"{prefix} {query}"

        async with self._engine.connect() as conn:  # type: ignore[union-attr]
            result = await conn.execute(text(explain_query), bound_params)
            columns = list(result.keys())
            rows = [dict(row._mapping) for row in result.fetchall()]

        return rows


    async def ping(self) -> bool:
        """Check if the connection is alive."""
        if not self._engine:
            return False
        try:
            async with self._engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return True
        except Exception:
            return False


    async def begin(self) -> None:
        self._assert_connected()
        if self._in_transaction:
            return
        self._conn = await self._engine.connect()  # type: ignore[union-attr]
        await self._conn.begin()
        self._in_transaction = True

    async def commit(self) -> None:
        if not self._in_transaction or not self._conn:
            return
        await self._conn.commit()
        await self._conn.close()
        self._conn = None
        self._in_transaction = False

    async def rollback(self) -> None:
        if not self._in_transaction or not self._conn:
            return
        await self._conn.rollback()
        await self._conn.close()
        self._conn = None
        self._in_transaction = False


    async def list_items(
        self, patterns: list[str] | None = None,
    ) -> list[WatchItem]:
        self._assert_connected()
        async with self._engine.connect() as conn:  # type: ignore[union-attr]
            table_names = await conn.run_sync(
                lambda sync_conn: inspect(sync_conn).get_table_names()
            )

        items = []
        for name in table_names:
            path = f"public.{name}"
            if patterns:
                if not any(fnmatch.fnmatch(path, p) or fnmatch.fnmatch(name, p) for p in patterns):
                    continue
            items.append(WatchItem(id=name, path=path))

        return items

    async def checksum(self, items: list[str]) -> list[ItemChecksum]:
        """Hash schema + row count for each table to detect changes."""
        self._assert_connected()
        checksums = []

        async with self._engine.connect() as conn:  # type: ignore[union-attr]
            for item_id in items:
                try:
                    cols = await conn.run_sync(
                        lambda sync_conn, t=item_id: inspect(sync_conn).get_columns(t)
                    )
                    result = await conn.execute(
                        text(f"SELECT COUNT(*) FROM {_quote_ident(item_id)}")
                    )
                    row = result.fetchone()
                    count = row[0] if row else 0

                    schema_repr = "|".join(
                        f"{c['name']}:{c['type']}" for c in cols
                    )
                    hash_input = f"{schema_repr}:{count}"
                    h = hashlib.sha256(hash_input.encode()).hexdigest()[:16]
                    checksums.append(ItemChecksum(id=item_id, hash=h))

                except Exception:
                    checksums.append(ItemChecksum(id=item_id, hash=""))

        return checksums


    def _assert_connected(self) -> None:
        if not self._engine:
            raise RuntimeError("Not connected. Call connect() first.")

    def _get_connection(self):
        """Return the transaction connection or a new one."""
        if self._in_transaction and self._conn:
            return _NoOpCtx(self._conn)
        return self._engine.connect()  # type: ignore[union-attr]


class _NoOpCtx:
    """Context manager that yields an existing connection without closing it."""

    def __init__(self, conn: AsyncConnection) -> None:
        self._conn = conn

    async def __aenter__(self) -> AsyncConnection:
        return self._conn

    async def __aexit__(self, *_: Any) -> None:
        pass


def _sanitize_url(url: str) -> str:
    """Remove password from URL for logging."""
    if "@" in url and ":" in url.split("@")[0]:
        parts = url.split("@")
        creds = parts[0]
        scheme_user = creds.rsplit(":", 1)[0]
        return f"{scheme_user}:****@{parts[1]}"
    return url


def _quote_ident(name: str) -> str:
    """Quote a table name to prevent SQL injection in introspection queries.

    Only allows alphanumeric + underscore + dot (for schema.table).
    """
    import re
    if not re.match(r"^[a-zA-Z_][a-zA-Z0-9_.]*$", name):
        raise ValueError(f"Invalid identifier: {name!r}")
    return f'"{name}"'


def _bind_params(params: list[Any] | None) -> dict[str, Any] | None:
    """Convert positional params to named params for SQLAlchemy text()."""
    if not params:
        return None
    return {f"p{i}": v for i, v in enumerate(params)}


def _get_pk_columns(
    sync_conn: Any, table: str, schema: str | None,
) -> set[str]:
    """Get primary key column names for a table."""
    try:
        pk = inspect(sync_conn).get_pk_constraint(table, schema=schema)
        return set(pk.get("constrained_columns", []))
    except Exception:
        return set()
