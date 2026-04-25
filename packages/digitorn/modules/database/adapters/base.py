"""Database adapter protocol and shared data types.

Every database backend (SQL, MongoDB, Redis, ...) implements this protocol.
The DatabaseModule dispatches to the adapter — it never talks to drivers directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class ColumnInfo:
    """Column metadata from introspection."""

    name: str
    type: str
    nullable: bool = True
    primary_key: bool = False
    default: str | None = None
    comment: str | None = None


@dataclass(frozen=True, slots=True)
class ForeignKey:
    """Foreign key relationship."""

    column: str
    referred_table: str
    referred_column: str


@dataclass(frozen=True, slots=True)
class IndexInfo:
    """Index metadata."""

    name: str
    columns: list[str]
    unique: bool = False


@dataclass(frozen=True, slots=True)
class TableInfo:
    """Table-level metadata."""

    name: str
    schema: str | None = None
    columns: list[ColumnInfo] = field(default_factory=list)
    foreign_keys: list[ForeignKey] = field(default_factory=list)
    indexes: list[IndexInfo] = field(default_factory=list)
    comment: str | None = None


@dataclass(frozen=True, slots=True)
class SchemaInfo:
    """Full schema introspection result."""

    tables: list[TableInfo]
    database: str = ""
    driver: str = ""


@dataclass(frozen=True, slots=True)
class TableStats:
    """Statistics for a single table."""

    name: str
    row_count: int
    size_bytes: int | None = None


@dataclass(frozen=True, slots=True)
class ExecuteResult:
    """Result of a DML/DDL statement."""

    rows_affected: int
    last_insert_id: Any | None = None


@dataclass(frozen=True, slots=True)
class FetchResult:
    """Result of a SELECT query."""

    columns: list[str]
    rows: list[dict[str, Any]]
    total_count: int | None = None


@dataclass(frozen=True, slots=True)
class WatchItem:
    """Item returned by list_items for the watcher."""

    id: str
    path: str


@dataclass(frozen=True, slots=True)
class ItemChecksum:
    """Checksum for a watched item."""

    id: str
    hash: str


@runtime_checkable
class DatabaseAdapter(Protocol):
    """Protocol that every database backend must implement.

    The adapter is stateful — it holds a connection/engine after ``connect()``.
    Call ``disconnect()`` to release resources.
    """

    @property
    def connected(self) -> bool:
        """Whether the adapter has an active connection."""
        ...

    @property
    def driver_name(self) -> str:
        """Short driver identifier, e.g. 'postgresql', 'sqlite', 'mongodb'."""
        ...


    async def connect(self, url: str, **options: Any) -> None:
        """Open a connection to the database."""
        ...

    async def disconnect(self) -> None:
        """Close the connection and release resources."""
        ...


    async def execute(
        self, query: str, params: list[Any] | None = None,
    ) -> ExecuteResult:
        """Execute a DML/DDL statement (INSERT, UPDATE, DELETE, CREATE, ...).

        Parameters are always positional (``$1``, ``?``, ``%s`` depending
        on the driver). The adapter translates as needed.
        """
        ...

    async def execute_many(
        self, query: str, params_list: list[dict[str, Any]],
    ) -> ExecuteResult:
        """Execute a statement with multiple parameter sets (batch).

        Much faster than calling ``execute()`` in a loop — uses the driver's
        native ``executemany`` underneath.
        """
        ...

    async def fetch(
        self,
        query: str,
        params: list[Any] | None = None,
        limit: int = 1000,
    ) -> FetchResult:
        """Execute a SELECT and return rows up to *limit*."""
        ...


    async def introspect(self, schema: str | None = None) -> SchemaInfo:
        """Return full schema metadata (tables, columns, FK, indexes)."""
        ...

    async def list_tables(self, schema: str | None = None) -> list[TableInfo]:
        """List tables with basic metadata."""
        ...

    async def get_columns(self, table: str) -> list[ColumnInfo]:
        """Get column details for a single table."""
        ...

    async def table_stats(self, table: str) -> TableStats:
        """Get row count and size for a table."""
        ...

    async def sample(self, table: str, limit: int = 5) -> FetchResult:
        """Fetch sample rows from a table."""
        ...


    async def explain(
        self, query: str, params: list[Any] | None = None, analyze: bool = False,
    ) -> list[dict[str, Any]]:
        """Return the query execution plan (EXPLAIN)."""
        ...

    async def ping(self) -> bool:
        """Check if the connection is alive. Returns True if healthy."""
        ...


    async def begin(self) -> None:
        """Start an explicit transaction."""
        ...

    async def commit(self) -> None:
        """Commit the current transaction."""
        ...

    async def rollback(self) -> None:
        """Roll back the current transaction."""
        ...


    async def list_items(
        self, patterns: list[str] | None = None,
    ) -> list[WatchItem]:
        """Enumerate watchable items (tables, views, ...) for the poller."""
        ...

    async def checksum(self, items: list[str]) -> list[ItemChecksum]:
        """Compute content hashes for the given items."""
        ...
